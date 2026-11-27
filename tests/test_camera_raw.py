"""Comprehensive tests for Slice R — Native Camera RAW Support.

Covers:
1. Synthetic 12-bit & 16-bit Bayer CinemaDNG single frames & numbered sequences + sidecar WAV
2. Real Bayer demosaic math: linear +1 EV / 2x ISO signal doubling, Kelvin/Tint WB shifts, Highlight Recovery
3. Preview proxy vs 10-bit ProRes 422 HQ export pixel parity (<= 2 RGB codes) & sequence frame accuracy
4. Synthetic .braw (+ .sidecar), .r3d (+ .RMD), .ari, .crm, .nev container metadata & missing-SDK guidance
5. Local mock Tier 2 CLI adapter contract: arg escaping, 16-bit rgb48le pipe, proxy & ProRes/H.264 export
6. Security: auth (403), path traversal, shell metacharacters, non-executable paths, malformed/truncated headers
7. Interchange (FCPXML/XMEML/EDL/OTIO) & DaVinci Resolve writer RAW loss disclosure
"""
from __future__ import annotations

import http.client
import sys
from array import array
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import media_links
import media_peaks
import media_proxy
import premiere
import raw_bayer
import raw_decode
import render_movie
import server
from interchange.api import handle as interchange_handle
from interchange.cut import build_cut
from tests import raw_fixtures


def _extract_rgb_frame(media_path: Path, *, frame_index: int = 0) -> tuple[int, int, bytes]:
    """Decode a single RGB24 frame from any video via FFmpeg."""
    probe = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
         '-show_entries', 'stream=width,height', '-of', 'json', str(media_path)],
        capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
    )
    stream = json.loads(probe.stdout)['streams'][0]
    w, h = int(stream['width']), int(stream['height'])
    cmd = [
        'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
        '-i', str(media_path),
        '-vf', f'select=eq(n\\,{frame_index})',
        '-vframes', '1',
        '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1',
    ]
    out = subprocess.run(cmd, capture_output=True, check=True, stdin=subprocess.DEVNULL).stdout
    assert len(out) == w * h * 3, f'Expected {w * h * 3} bytes, got {len(out)}'
    return w, h, out


def _sample_patch_rgb(w: int, h: int, rgb: bytes, col: int, row: int) -> tuple[float, float, float]:
    """Average the interior of patch (col in 0..2, row in 0..1) avoiding edges and top marker bar."""
    col_w = w // 3
    row_h = h // 2
    x0 = col * col_w + max(4, col_w // 4)
    x1 = (col + 1) * col_w - max(4, col_w // 4)
    y0 = row * row_h + max(6, row_h // 4)
    y1 = (row + 1) * row_h - max(4, row_h // 4)
    sr = sg = sb = count = 0
    for y in range(y0, y1):
        row_off = y * w * 3
        for x in range(x0, x1):
            p = row_off + x * 3
            sr += rgb[p]
            sg += rgb[p + 1]
            sb += rgb[p + 2]
            count += 1
    return (sr / count, sg / count, sb / count)


def _sample_rgb48_patch(w: int, h: int, rgb48: bytes, col: int, row: int) -> tuple[float, float, float]:
    col_w = w // 3
    row_h = h // 2
    x0 = col * col_w + max(4, col_w // 4)
    x1 = (col + 1) * col_w - max(4, col_w // 4)
    y0 = row * row_h + max(6, row_h // 4)
    y1 = (row + 1) * row_h - max(4, row_h // 4)
    sr = sg = sb = count = 0
    for y in range(y0, y1):
        row_off = y * w * 6
        for x in range(x0, x1):
            r, g, b = struct.unpack_from('<HHH', rgb48, row_off + x * 6)
            sr += r
            sg += g
            sb += b
            count += 1
    return (sr / (count * 65535.0), sg / (count * 65535.0), sb / (count * 65535.0))


class _IsolatedDataDirMixin:
    """Redirect Splice's data/media directories into a clean temporary directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='splice-raw-test-'))
        self.data_dir = self.tmp / 'data'
        self.media_dir = self.data_dir / 'media'
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self._patches = [
            mock.patch.object(premiere, 'MEDIA_ROOT', self.media_dir),
            mock.patch.object(media_links, 'MEDIA_ROOT', self.media_dir),
            mock.patch.object(media_proxy, 'MEDIA_ROOT', self.media_dir),
            mock.patch.object(render_movie, 'MEDIA_ROOT', self.media_dir),
            mock.patch.object(server, 'MEDIA_ROOT', self.media_dir),
            mock.patch.object(media_links, 'REGISTRY', self.data_dir / 'media-links.json'),
            mock.patch('raw_decode.config_path', return_value=self.data_dir / 'raw-decoders.json'),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestCinemaDNGBayer(_IsolatedDataDirMixin, unittest.TestCase):
    def test_12bit_and_16bit_dng_tags_and_linear_exposure_doubling(self):
        for bit_depth, big_endian in ((12, False), (16, False), (16, True)):
            dng = raw_fixtures.write_synthetic_dng(
                self.tmp / f'chart_{bit_depth}_{"be" if big_endian else "le"}.dng',
                width=96, height=64, bit_depth=bit_depth,
                frame_rate=(24, 1), timecode=(1, 2, 3, 4),
                reel='R012', camera='Splice Test Cam',
                big_endian=big_endian,
            )
            meta = raw_bayer.parse_dng(dng)
            self.assertEqual(meta['width'], 96)
            self.assertEqual(meta['height'], 64)
            self.assertEqual(meta['bitsPerSample'], bit_depth)
            self.assertEqual(list(meta['cfaPattern']), [0, 1, 1, 2])
            self.assertEqual(meta['timecode'], '01:02:03:04')
            self.assertEqual(meta['reel'], 'R012')
            self.assertEqual(meta['camera'], 'Splice Test Cam')
            self.assertAlmostEqual(float(meta['fps']), 24.0, places=3)

            # Decode neutral mid-grey patch in Linear gamma at 0 EV and +1 EV
            w0, h0, buf0 = raw_bayer.decode_dng_frame(dng, {
                'decodeUsing': 'clip', 'exposure': 0.0, 'iso': 800,
                'kelvin': 6504, 'tint': 0.0, 'colorSpace': 'linear', 'gamma': 'linear',
            }, scale=1)
            w1, h1, buf1 = raw_bayer.decode_dng_frame(dng, {
                'decodeUsing': 'clip', 'exposure': 1.0, 'iso': 800,
                'kelvin': 6504, 'tint': 0.0, 'colorSpace': 'linear', 'gamma': 'linear',
            }, scale=1)
            w2, h2, buf2 = raw_bayer.decode_dng_frame(dng, {
                'decodeUsing': 'clip', 'exposure': 0.0, 'iso': 1600,
                'kelvin': 6504, 'tint': 0.0, 'colorSpace': 'linear', 'gamma': 'linear',
            }, scale=1)
            self.assertEqual((w0, h0), (96, 64))
            g0 = _sample_rgb48_patch(w0, h0, buf0, 0, 0)
            g1 = _sample_rgb48_patch(w1, h1, buf1, 0, 0)
            g2 = _sample_rgb48_patch(w2, h2, buf2, 0, 0)
            # +1 EV and 2x ISO must double linear signal (~0.18 -> ~0.36)
            for ch in range(3):
                self.assertAlmostEqual(g0[ch], 0.18, delta=0.015)
                self.assertAlmostEqual(g1[ch] / g0[ch], 2.0, delta=0.03)
                self.assertAlmostEqual(g2[ch] / g0[ch], 2.0, delta=0.03)

    def test_white_balance_kelvin_tint_and_highlight_recovery(self):
        dng = raw_fixtures.write_synthetic_dng(self.tmp / 'wb.dng', width=96, height=64, bit_depth=12)
        w, h, cool = raw_bayer.decode_dng_frame(dng, {
            'decodeUsing': 'clip', 'kelvin': 3200, 'tint': 0.0,
            'colorSpace': 'rec709', 'gamma': 'linear',
        }, scale=1)
        _, _, warm = raw_bayer.decode_dng_frame(dng, {
            'decodeUsing': 'clip', 'kelvin': 8500, 'tint': 0.0,
            'colorSpace': 'rec709', 'gamma': 'linear',
        }, scale=1)
        c_grey = _sample_rgb48_patch(w, h, cool, 0, 0)
        w_grey = _sample_rgb48_patch(w, h, warm, 0, 0)
        # Warmer Kelvin raises R/B ratio relative to cooler Kelvin
        self.assertGreater(w_grey[0] / max(1e-5, w_grey[2]), c_grey[0] / max(1e-5, c_grey[2]) * 1.4)

        _, _, tint_neg = raw_bayer.decode_dng_frame(dng, {
            'decodeUsing': 'clip', 'kelvin': 5600, 'tint': -60.0,
            'colorSpace': 'rec709', 'gamma': 'linear',
        }, scale=1)
        _, _, tint_pos = raw_bayer.decode_dng_frame(dng, {
            'decodeUsing': 'clip', 'kelvin': 5600, 'tint': 60.0,
            'colorSpace': 'rec709', 'gamma': 'linear',
        }, scale=1)
        tn = _sample_rgb48_patch(w, h, tint_neg, 0, 0)
        tp = _sample_rgb48_patch(w, h, tint_pos, 0, 0)
        # Positive tint (magenta) lowers Green relative to Red+Blue compared to negative tint (green)
        gn_ratio = tn[1] / ((tn[0] + tn[2]) * 0.5)
        gp_ratio = tp[1] / ((tp[0] + tp[2]) * 0.5)
        self.assertGreater(gn_ratio, gp_ratio * 1.05)

        # Highlight recovery on patch (1, 1) where Red is clipped (>1.0) and G/B are below clip
        _, _, hr_off = raw_bayer.decode_dng_frame(dng, {
            'decodeUsing': 'clip', 'exposure': 0.5, 'kelvin': 6504, 'tint': 0.0,
            'colorSpace': 'linear', 'gamma': 'linear', 'highlightRecovery': False,
        }, scale=1)
        _, _, hr_on = raw_bayer.decode_dng_frame(dng, {
            'decodeUsing': 'clip', 'exposure': 0.5, 'kelvin': 6504, 'tint': 0.0,
            'colorSpace': 'linear', 'gamma': 'linear', 'highlightRecovery': True,
        }, scale=1)
        p_off = _sample_rgb48_patch(w, h, hr_off, 1, 1)
        p_on = _sample_rgb48_patch(w, h, hr_on, 1, 1)
        # Highlight recovery rolls clipped channel smoothly toward neutral rather than hard-clipping chroma
        self.assertGreater(p_on[2], p_off[2] + 0.02)

    def test_sequence_detection_preview_export_parity_and_frame_accuracy(self):
        seq_dir = self.tmp / 'A001_seq'
        first = raw_fixtures.write_cinemadng_sequence(
            seq_dir, prefix='A001_', frames=12, fps=24, bit_depth=12,
            width=128, height=96, with_wav=True, reel='A001', timecode=(1, 0, 0, 0),
        )
        # Folder listing collapses sequence into one entry
        files = media_links.folder_files(str(seq_dir))
        dng_entries = [p for p, _ in files if p.endswith('.dng')]
        self.assertEqual(len(dng_entries), 1)

        linked = media_links.link_many([str(first)], validate=media_proxy.decodable)[0]
        self.assertNotIn('error', linked)
        src = linked['src']
        info = media_proxy.describe(src)
        self.assertTrue(info['raw'])
        self.assertEqual(info['rawFormat'], 'cinemadng')
        self.assertTrue(info['rawDecoderAvailable'])
        self.assertEqual(info['width'], 128)
        self.assertEqual(info['height'], 96)
        self.assertAlmostEqual(info['fps'], 24.0, places=2)
        self.assertAlmostEqual(info['duration'], 0.5, places=2)
        self.assertEqual(info['reel'], 'A001')
        self.assertEqual(info['timecode'], '01:00:00:00')
        self.assertGreaterEqual(info['audioStreams'], 1)
        self.assertEqual(info['audioChannels'], 2)

        # Sidecar WAV waveform & audition work cleanly
        peaks = media_peaks.waveform(src)
        self.assertGreater(len(peaks), 10)
        self.assertGreater(max(peaks), 0.1)
        wav_bytes = media_proxy.audition_audio(src, 0, 1.0, False, 0)
        self.assertTrue(wav_bytes.startswith(b'RIFF'))

        # Verify preview vs 10-bit ProRes 422 HQ export pixel parity <= 2 RGB codes
        # for both Camera Metadata AND Clip overrides (+1 EV, 7200K, tint +10)
        for label, raw_settings in (
            ('camera-default', {'decodeUsing': 'camera', 'debayerQuality': 'full'}),
            ('clip-override', {
                'decodeUsing': 'clip', 'iso': 800, 'exposure': 1.0,
                'kelvin': 7200, 'tint': 10, 'colorSpace': 'rec709',
                'gamma': 'rec709', 'highlightRecovery': True, 'debayerQuality': 'full',
            }),
        ):
            proxy_src = media_proxy.proxy(src, raw_settings=raw_settings)
            proxy_path = media_links.resolve_media(proxy_src)
            export_path = self.tmp / f'export_{label}.mov'
            project = {
                'name': f'RAW Parity {label}',
                'width': 128, 'height': 96, 'fps': 24,
                'media': [{
                    'id': 'm1', 'name': first.name, 'src': src, 'type': 'video',
                    'duration': 0.5, 'width': 128, 'height': 96, 'fps': 24,
                    'raw': True, 'rawFormat': 'cinemadng', 'rawDecoderAvailable': True,
                    'audioChannels': 2,
                }],
                'clips': [{
                    'id': 'c1', 'type': 'video', 'track': 1, 'mediaId': 'm1',
                    'start': 0.0, 'in': 0.0, 'duration': 0.5, 'rawSettings': raw_settings,
                }],
            }
            render_movie.render_to_file(
                project, export_path,
                options={'codec': 'prores', 'profile': 'hq', 'limiter': False},
            )
            self.assertTrue(export_path.is_file())
            pw, ph, p_rgb = _extract_rgb_frame(proxy_path, frame_index=5)
            ew, eh, e_rgb = _extract_rgb_frame(export_path, frame_index=5)
            self.assertEqual((pw, ph), (128, 96))
            self.assertEqual((ew, eh), (128, 96))
            max_delta = 0.0
            for row in range(2):
                for col in range(3):
                    pr = _sample_patch_rgb(pw, ph, p_rgb, col, row)
                    er = _sample_patch_rgb(ew, eh, e_rgb, col, row)
                    delta = max(abs(pr[c] - er[c]) for c in range(3))
                    max_delta = max(max_delta, delta)
                    self.assertLessEqual(
                        delta, 2.0,
                        f'{label} patch ({col},{row}) preview={pr} vs export={er} delta={delta:.2f} > 2',
                    )

            # Frame accuracy: frame 5 marker bar is at x = 20..23, y = 0..3
            # Check that the marker pixel is bright white in frame 5 and dark/patch-colored in frame 0
            _, _, e_rgb0 = _extract_rgb_frame(export_path, frame_index=0)
            p_at_21_f5 = e_rgb[(1 * ew + 21) * 3]
            p_at_21_f0 = e_rgb0[(1 * ew + 21) * 3]
            self.assertGreater(p_at_21_f5, 220)
            self.assertLess(p_at_21_f0, p_at_21_f5 - 30)

        # Disabled RAW clip must NOT decode RAW and must export black video
        disabled_out = self.tmp / 'disabled_raw.mp4'
        disabled_project = {
            'name': 'Disabled RAW',
            'width': 128, 'height': 96, 'fps': 24,
            'media': [{
                'id': 'm1', 'name': first.name, 'src': src, 'type': 'video',
                'duration': 0.5, 'width': 128, 'height': 96, 'fps': 24,
                'raw': True, 'rawFormat': 'cinemadng', 'rawDecoderAvailable': True,
            }],
            'clips': [{
                'id': 'c1', 'type': 'video', 'track': 1, 'mediaId': 'm1',
                'start': 0.0, 'in': 0.0, 'duration': 0.5, 'enabled': False,
            }],
        }
        with mock.patch('raw_decode.decode_to_intermediate', side_effect=AssertionError('Disabled clip must not decode')):
            render_movie.render_to_file(disabled_project, disabled_out, options={'codec': 'h264', 'limiter': False})
        w, h, rgb = _extract_rgb_frame(disabled_out, frame_index=2)
        self.assertLessEqual(max(rgb), 18)


class TestTier2ContainersAndAdapterContract(_IsolatedDataDirMixin, unittest.TestCase):
    def test_synthetic_braw_r3d_ari_crm_nev_metadata_and_missing_sdk_guidance(self):
        braw = raw_fixtures.write_synthetic_braw(self.tmp / 'A001_C001.braw', with_audio=True, with_sidecar=True)
        r3d = raw_fixtures.write_synthetic_r3d(self.tmp / 'A002_C001.r3d', with_rmd=True)
        ari = raw_fixtures.write_synthetic_ari(self.tmp / 'C001_000001.ari')
        crm = raw_fixtures.write_synthetic_crm(self.tmp / 'D001_C001.crm')
        nev = raw_fixtures.write_synthetic_nev(self.tmp / 'E001_C001.nev')

        # Ensure vendor SDKs are treated as absent for missing-SDK check
        missing_backend = {'available': False, 'path': None, 'backend': None, 'version': None, 'guidance': 'Install Blackmagic RAW SDK or braw-decode CLI in Settings -> RAW Decoders.'}
        with mock.patch('raw_decode._resolve_backend', return_value=missing_backend):
            b_info = raw_decode.probe_raw(braw)
            self.assertEqual(b_info['rawFormat'], 'braw')
            self.assertFalse(b_info['rawDecoderAvailable'])
            self.assertIn('Blackmagic', b_info['rawDecoderMessage'])
            self.assertEqual(b_info['width'], 160)
            self.assertEqual(b_info['height'], 90)
            self.assertAlmostEqual(b_info['fps'], 24.0, places=2)
            self.assertEqual(b_info['timecode'], '01:02:03:04')
            self.assertEqual(b_info['reel'], 'B001')
            self.assertGreaterEqual(b_info['audioStreams'], 1)
            # .sidecar JSON defaults picked up
            self.assertEqual(b_info['rawDefaults']['iso'], 1600)
            self.assertEqual(b_info['rawDefaults']['kelvin'], 4800)

            r_info = raw_decode.probe_raw(r3d)
            self.assertEqual(r_info['rawFormat'], 'r3d')
            self.assertFalse(r_info['rawDecoderAvailable'])
            self.assertEqual(r_info['width'], 4096)
            self.assertEqual(r_info['height'], 2160)
            self.assertEqual(r_info['reel'], 'A002')
            self.assertEqual(r_info['timecode'], '01:05:10:12')
            self.assertEqual(r_info['rawDefaults']['iso'], 1280)
            self.assertEqual(r_info['rawDefaults']['kelvin'], 5200)

            self.assertEqual(r_info['rawMetadata']['frameCount'], 12)
            self.assertAlmostEqual(r_info['duration'], 12 * 1001 / 24000, places=4)
            self.assertEqual(r_info['rate'], '24000/1001')
            self.assertNotIn('timecode', r_info['rawEstimated'])

            a_info = raw_decode.probe_raw(ari)
            self.assertEqual(a_info['rawFormat'], 'arriraw')
            self.assertEqual(a_info['width'], 2880)
            self.assertEqual(a_info['height'], 1620)
            self.assertEqual(a_info['rawMetadata']['camera'], 'ALEXA Synthetic')
            # A lone ARRIRAW frame carries no timecode or frame rate Splice can read: say so, never invent.
            self.assertIsNone(a_info['timecode'])
            self.assertIn('timecode', a_info['rawEstimated'])
            self.assertIn('fps', a_info['rawEstimated'])
            self.assertTrue(a_info['still'])

            c_info = raw_decode.probe_raw(crm)
            self.assertEqual(c_info['rawFormat'], 'canon_crm')
            self.assertEqual(c_info['timecode'], '03:00:00:00')
            self.assertGreaterEqual(c_info['audioStreams'], 1)

            n_info = raw_decode.probe_raw(nev)
            self.assertEqual(n_info['rawFormat'], 'nraw')
            self.assertEqual(n_info['timecode'], '04:00:00:00')
            self.assertGreaterEqual(n_info['audioStreams'], 1)

            # Missing-SDK proxy still succeeds cleanly (slate + real audio) and export gives actionable error
            slate = self.tmp / 'braw_slate.mp4'
            raw_decode.decode_to_proxy(braw, slate)
            self.assertTrue(slate.is_file() and slate.stat().st_size > 0)
            with self.assertRaises(raw_decode.RawError) as ctx:
                raw_decode.decode_to_intermediate(braw, self.tmp / 'fail.mov')
            self.assertIn('RAW Decoders', str(ctx.exception))

    def test_mock_tier2_cli_adapter_contract_proxy_and_full_res_exports(self):
        # Filename with spaces, quotes, and parens verifies zero shell injection / exact argv escaping
        braw = raw_fixtures.write_synthetic_braw(
            self.tmp / "clip 'quoted' (1).braw",
            width=128, height=96, fps=24, duration=0.5, with_audio=True, with_sidecar=True,
        )
        mock_cli = raw_fixtures.write_mock_raw_cli(self.tmp / 'mock-braw-decode')
        log_file = self.tmp / 'mock_argv.json'

        raw_decode.save_decoder_config({'braw': str(mock_cli)})
        status = raw_decode.decoder_status()
        braw_entry = next(d for d in status['decoders'] if d['id'] == 'braw')
        self.assertTrue(braw_entry['available'])
        self.assertEqual(braw_entry['path'], str(mock_cli.resolve()))

        linked = media_links.link_many([str(braw)], validate=media_proxy.decodable)[0]
        src = linked['src']
        info = media_proxy.describe(src)
        self.assertTrue(info['raw'])
        self.assertTrue(info['rawDecoderAvailable'])

        with mock.patch.dict(os.environ, {'SPLICE_MOCK_DECODER_LOG': str(log_file)}):
            settings_lo = {
                'decodeUsing': 'clip', 'iso': 800, 'exposure': 0.0,
                'kelvin': 5600, 'tint': 0.0, 'colorSpace': 'rec709',
                'gamma': 'rec709', 'highlightRecovery': True, 'debayer': 'half',
            }
            proxy_lo_src = media_proxy.proxy(src, raw_settings=settings_lo)
            logged = json.loads(log_file.read_text(encoding='utf-8'))
            self.assertEqual(logged['args']['input'], str(braw.resolve()))
            self.assertEqual(logged['args']['iso'], 800)
            self.assertEqual(logged['args']['kelvin'], 5600)
            self.assertEqual(logged['args']['quality'], 'half')
            self.assertEqual(logged['args']['highlight_recovery'], 1)
            self.assertEqual(logged['args']['pix_fmt'], 'rgb48le')

            settings_hi = {
                'decodeUsing': 'clip', 'iso': 1600, 'exposure': 0.5,
                'kelvin': 7500, 'tint': 0.0, 'colorSpace': 'rec709',
                'gamma': 'rec709', 'highlightRecovery': False, 'debayer': 'full',
            }
            proxy_hi_src = media_proxy.proxy(src, raw_settings=settings_hi)
            self.assertNotEqual(proxy_lo_src, proxy_hi_src)

            lo_w, lo_h, rgb_lo = _extract_rgb_frame(media_links.resolve_media(proxy_lo_src), frame_index=2)
            hi_w, hi_h, rgb_hi = _extract_rgb_frame(media_links.resolve_media(proxy_hi_src), frame_index=2)
            self.assertEqual((lo_w, lo_h), (64, 48), 'half debayer asks the decoder for half size')
            self.assertEqual((hi_w, hi_h), (128, 96))
            centre_lo = rgb_lo[((lo_h // 2) * lo_w + lo_w // 2) * 3]
            centre_hi = rgb_hi[((hi_h // 2) * hi_w + hi_w // 2) * 3]
            # Higher ISO + exposure + warmer Kelvin noticeably brightens and warms Red
            self.assertGreater(centre_hi, centre_lo + 40)

            # Full-res ProRes AND H.264 exports stream rgb48le through mock CLI at full debayer
            project = {
                'name': 'Mock BRAW Export',
                'width': 128, 'height': 96, 'fps': 24,
                'media': [{
                    'id': 'm1', 'name': braw.name, 'src': src, 'type': 'video',
                    'duration': 0.5, 'width': 128, 'height': 96, 'fps': 24,
                    'raw': True, 'rawFormat': 'braw', 'rawDecoderAvailable': True, 'audioChannels': 2,
                }],
                'clips': [{
                    'id': 'c1', 'type': 'video', 'track': 1, 'mediaId': 'm1',
                    'start': 0.0, 'in': 0.0, 'duration': 0.5, 'rawSettings': settings_hi,
                }],
            }
            prores_out = self.tmp / 'mock_braw_prores.mov'
            h264_out = self.tmp / 'mock_braw_h264.mp4'
            render_movie.render_to_file(project, prores_out, options={'codec': 'prores', 'profile': 'hq', 'limiter': False})
            logged_export = json.loads(log_file.read_text(encoding='utf-8'))
            self.assertEqual(logged_export['args']['quality'], 'full')
            render_movie.render_to_file(project, h264_out, options={'codec': 'h264', 'limiter': False})
            self.assertTrue(prores_out.is_file() and prores_out.stat().st_size > 1000)
            self.assertTrue(h264_out.is_file() and h264_out.stat().st_size > 500)


def _marker_frame(rgb: bytes, width: int) -> int | None:
    """The synthetic sequence writes a white 4x4 marker at x = 4 * frame on rows 0..3."""
    bright = [x for x in range(width - 8) if rgb[(1 * width + x) * 3] > 200 and rgb[(1 * width + x) * 3 + 1] > 200]
    return bright[0] // 4 if bright else None


def _children_of_this_process() -> list[int]:
    found = []
    for entry in os.listdir('/proc'):
        if entry.isdigit():
            try:
                with open(f'/proc/{entry}/stat', encoding='utf-8') as fh:
                    fields = fh.read().rsplit(')', 1)[1].split()
            except OSError:
                continue
            if int(fields[1]) == os.getpid() and fields[0] != 'Z':
                found.append(int(entry))
    return found


class TestRawDecodePipeline(_IsolatedDataDirMixin, unittest.TestCase):
    def _sequence(self, frames=24, width=128, height=96, with_wav=True):
        return raw_fixtures.write_cinemadng_sequence(
            self.tmp / 'A003_seq', prefix='A003_', frames=frames, fps=24, bit_depth=12,
            width=width, height=height, with_wav=with_wav, reel='A003', timecode=(1, 0, 0, 0))

    def test_export_decodes_only_the_frames_the_cut_reads_and_stays_frame_accurate(self):
        first = self._sequence()
        src = media_links.link_many([str(first)], validate=media_proxy.decodable)[0]['src']
        info = media_proxy.describe(src)
        self.assertEqual(info['rawMetadata']['frameCount'], 24)
        project = {
            'name': 'Range', 'width': 128, 'height': 96, 'fps': 24,
            'media': [{**info, 'id': 'm1', 'name': first.name, 'src': src}],
            'clips': [{'id': 'c1', 'type': 'video', 'track': 1, 'mediaId': 'm1', 'start': 0.0,
                       'in': 15 / 24, 'duration': 6 / 24, 'transition': 'none'}],
        }
        calls = []
        real = raw_decode.decode_to_intermediate

        def spy(*args, **kwargs):
            calls.append(kwargs)
            return real(*args, **kwargs)

        out = self.tmp / 'range.mov'
        with mock.patch('raw_decode.decode_to_intermediate', side_effect=spy):
            render_movie.render_to_file(project, out, options={'codec': 'prores', 'profile': 'hq', 'limiter': False})
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(calls[0]['start_frame'], 15)
        self.assertLess(calls[0]['frame_count'], 24, 'export must not develop the whole roll')
        self.assertGreaterEqual(calls[0]['start_frame'] + calls[0]['frame_count'], 21)
        for n in range(6):
            width, _, rgb = _extract_rgb_frame(out, frame_index=n)
            self.assertEqual(_marker_frame(rgb, width), 15 + n, f'export frame {n}')
        # The sound is pulled from the same offset of the sidecar WAV (480 Hz sine starts at 0).
        probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries',
                                'stream=codec_type', '-of', 'csv=p=0', str(out)], capture_output=True, text=True)
        self.assertEqual(probe.stdout.strip(), 'audio')

    def test_ffmpeg_unpacked_dng_matches_the_builtin_unpacker(self):
        for cfa in ((0, 1, 1, 2), (1, 0, 2, 1), (2, 1, 1, 0)):
            dng = raw_fixtures.write_synthetic_dng(self.tmp / f'cfa{cfa[0]}{cfa[1]}.dng', width=96, height=64,
                                                   bit_depth=12, cfa_pattern=cfa, black_level=200)
            settings = {'decodeUsing': 'clip', 'kelvin': 4300, 'tint': 12, 'exposure': 0.5}
            builtin = raw_decode.decode_dng(dng, settings, 1)
            via_ffmpeg = raw_decode.decode_dng(dng, settings, 1, via_ffmpeg=True)
            self.assertEqual(builtin[:2], via_ffmpeg[:2])
            a, b = array('H'), array('H')
            a.frombytes(builtin[2])
            b.frombytes(via_ffmpeg[2])
            worst = max(abs(x - y) for x, y in zip(a, b))
            self.assertLessEqual(worst, 257, f'CFA {cfa}: {worst} 16-bit codes (> one 8-bit code)')

    def test_compressed_dng_routes_to_ffmpeg_and_unknown_compression_explains_itself(self):
        dng = raw_fixtures.write_synthetic_dng(self.tmp / 'C001_000001.dng', width=96, height=64, bit_depth=12)
        meta = raw_bayer.parse_dng(dng)
        with mock.patch('raw_bayer.parse_dng', return_value={**meta, 'compression': 7, 'compressed': True,
                                                              'builtinDecodable': False}):
            info = raw_decode.probe_raw(dng)
            self.assertTrue(info['rawDecoderAvailable'])
            self.assertIn('FFmpeg DNG', info['rawDecoderName'])
        raw_decode._probe_cache.clear()
        missing = {'available': False, 'path': None, 'activeBackend': None, 'version': None}
        with mock.patch('raw_bayer.parse_dng', return_value={**meta, 'compression': 34892, 'compressed': True,
                                                              'builtinDecodable': False}), \
                mock.patch('raw_decode._resolve_backend', return_value=missing):
            info = raw_decode.probe_raw(dng)
        self.assertFalse(info['rawDecoderAvailable'])
        self.assertIn('RAW Decoders', info['rawDecoderMessage'])

    def test_prores_raw_chain_on_a_bayer_standin_doubles_linear_light_and_warms(self):
        dng = raw_fixtures.write_synthetic_dng(self.tmp / 'standin.dng', width=96, height=64, bit_depth=16)
        bayer = self.tmp / 'standin.bayer'
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(dng), '-f', 'rawvideo', '-pix_fmt', 'bayer_rggb16le',
                        str(bayer)], check=True, stdin=subprocess.DEVNULL)
        stand_in = ['-f', 'rawvideo', '-pix_fmt', 'bayer_rggb16le', '-s', '96x64', '-framerate', '24',
                    '-stream_loop', '-1', '-i', str(bayer)]
        info = {'rate': '24/1', 'duration': 0.25, 'width': 96, 'height': 64, 'audioStreams': 0}

        def grey(settings):
            out = self.tmp / f"prr-{len(list(self.tmp.glob('prr-*')))}.mov"
            raw_decode._decode_prores_raw(dng, out, settings, info, is_intermediate=True, input_args=stand_in)
            frame = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(out), '-frames:v', '1', '-f', 'rawvideo',
                                    '-pix_fmt', 'rgb48le', '-'], capture_output=True, check=True).stdout
            return _sample_rgb48_patch(96, 64, frame, 0, 0)

        base = grey({'decodeUsing': 'clip', 'gamma': 'linear'})
        brighter = grey({'decodeUsing': 'clip', 'gamma': 'linear', 'exposure': 1.0})
        warm = grey({'decodeUsing': 'clip', 'gamma': 'linear', 'kelvin': 9000})
        for channel in range(3):
            self.assertAlmostEqual(brighter[channel] / base[channel], 2.0, delta=0.05)
        self.assertGreater(warm[0] / warm[2], base[0] / base[2] * 1.2)
        self.assertGreater(grey({'decodeUsing': 'clip', 'gamma': 'rec709'})[1], base[1] * 1.8)

    def test_cancel_during_a_long_decode_stops_every_child_process(self):
        first = self._sequence(frames=24, with_wav=False)
        cancelled = threading.Event()
        beats = []

        def heartbeat():
            beats.append(1)
            if len(beats) == 4:
                cancelled.set()

        with self.assertRaises(InterruptedError):
            raw_decode.decode_to_intermediate(first, self.tmp / 'cancel.mov', {'decodeUsing': 'camera'},
                                              cancelled=cancelled, heartbeat=heartbeat)
        self.assertLess(len(beats), 8)
        self.assertEqual(_children_of_this_process(), [])

    def _cli(self, body: str) -> Path:
        path = self.tmp / 'bad-decoder'
        path.write_text(f'#!{sys.executable}\nimport sys\n{body}\n', encoding='utf-8')
        path.chmod(0o755)
        return path

    def test_broken_and_chatty_decoders_fail_clearly_or_finish(self):
        braw = raw_fixtures.write_synthetic_braw(self.tmp / 'A009_C001.braw', width=64, height=48, duration=0.25,
                                                 with_audio=False, with_sidecar=False)
        cases = {
            'short frame then exit 3': ("sys.stdout.buffer.write(b'\\0' * 1000); sys.stdout.flush(); "
                                        "sys.stderr.write('licence expired'); sys.exit(3)", 'licence expired'),
            'too few frames': ("sys.stdout.buffer.write(b'\\0' * (64 * 48 * 6 * 2))", 'delivered 2 of 6 frames'),
        }
        for label, (body, expect) in cases.items():
            raw_decode.save_decoder_config({'braw': str(self._cli(body))})
            raw_decode._probe_cache.clear()
            with self.assertRaises(raw_decode.RawError, msg=label) as ctx:
                raw_decode.decode_to_intermediate(braw, self.tmp / 'bad.mov')
            self.assertIn(expect, str(ctx.exception), label)
            self.assertEqual(_children_of_this_process(), [], label)
        chatty = self._cli("sys.stderr.write('x' * 2_000_000); sys.stderr.flush()\n"
                           "sys.stdout.buffer.write(b'\\x10\\x00' * (64 * 48 * 3 * 6))")
        raw_decode.save_decoder_config({'braw': str(chatty)})
        raw_decode._probe_cache.clear()
        worker = threading.Thread(target=raw_decode.decode_to_intermediate, args=(braw, self.tmp / 'chatty.mov'))
        worker.start()
        worker.join(60)
        self.assertFalse(worker.is_alive(), 'a decoder writing 2 MB to stderr must not deadlock the reader')
        self.assertTrue((self.tmp / 'chatty.mov').stat().st_size > 0)

    def test_a_sequence_with_a_missing_frame_number_says_so(self):
        first = self._sequence(frames=6, with_wav=False)
        (first.parent / 'A003_000004.dng').unlink()
        info = raw_decode.probe_raw(first)
        self.assertEqual(info['rawMetadata']['frameCount'], 5)
        self.assertEqual(info['rawMetadata']['missingFrames'], 1)

    def test_an_sdk_library_alone_is_reported_but_not_offered_as_a_decoder(self):
        library = self.tmp / 'libBlackmagicRawAPI.so'
        library.write_bytes(b'\x7fELF')
        library.chmod(0o644)
        with mock.patch.dict(os.environ, {'SPLICE_BRAW_SDK': str(library)}):
            row = next(d for d in raw_decode.decoder_status()['decoders'] if d['id'] == 'braw')
        self.assertFalse(row['available'], 'Splice cannot drive the SDK library without a command-line bridge')
        self.assertEqual(row['sdkPath'], str(library.resolve()))
        self.assertIn('command-line bridge', row['guidance'])

    def test_unknown_metadata_is_flagged_never_invented(self):
        r3d = raw_fixtures.write_synthetic_r3d(self.tmp / 'A004_C002_0101AB_001.R3D', with_rmd=False)
        info = raw_decode.probe_raw(r3d)
        self.assertIsNone(info['timecode'])
        self.assertIn('timecode', info['rawEstimated'])
        self.assertEqual(info['reel'], 'A004')
        self.assertEqual(info['rawMetadata']['reelSource'], 'file name')
        self.assertNotIn('fps', info['rawEstimated'])
        self.assertNotIn('duration', info['rawEstimated'])
        braw = raw_fixtures.write_synthetic_braw(self.tmp / 'B004_C001.braw', with_sidecar=False)
        self.assertEqual(raw_decode.probe_raw(braw)['rawEstimated'], [])
        ffprobe = raw_decode._ffprobe_optional(r3d)
        if ffprobe and raw_decode._ffprobe_streams(ffprobe, 'video'):
            video = raw_decode._ffprobe_streams(ffprobe, 'video')[0]
            self.assertEqual((video.get('width'), video.get('height')), (info['width'], info['height']),
                             "Splice's R3D header read disagrees with FFmpeg's r3d demuxer")

    def test_hostile_headers_are_bounded(self):
        import time
        many = bytearray(b'II*\x00\x08\x00\x00\x00') + struct.pack('<H', 65535) + b'\x00' * (65535 * 12 + 4)
        (self.tmp / 'many.dng').write_bytes(many)
        deep = b''
        for _ in range(200):
            deep = struct.pack('>I4s', 8 + len(deep), b'moov') + deep
        (self.tmp / 'deep.braw').write_bytes(struct.pack('>I4s4s', 12, b'ftyp', b'qt  ') + deep)
        huge = struct.pack('>I4s4s', 12, b'ftyp', b'qt  ') + struct.pack('>I4sQ', 1, b'moov', 2 ** 40)
        (self.tmp / 'huge.braw').write_bytes(huge)
        red = bytearray(200)
        red[4:8] = b'RED2'
        struct.pack_into('>I4sI', red, 144, 56, b'REOB', 2 ** 31)
        (self.tmp / 'liar.r3d').write_bytes(bytes(red))
        for name in ('many.dng', 'deep.braw', 'huge.braw', 'liar.r3d'):
            began = time.monotonic()
            try:
                raw_decode.probe_raw(self.tmp / name)
            except ValueError:
                pass
            self.assertLess(time.monotonic() - began, 5, name)

    def test_container_sound_feeds_waveforms_and_an_attached_proxy_exports_without_a_decoder(self):
        braw = raw_fixtures.write_synthetic_braw(self.tmp / 'B005_C001.braw', width=128, height=96, duration=0.5)
        src = media_links.link_many([str(braw)], validate=media_proxy.decodable)[0]['src']
        missing = {'available': False, 'path': None, 'activeBackend': None, 'version': None, 'installHint': 'x'}
        with mock.patch('raw_decode._resolve_backend', return_value=missing):
            raw_decode._probe_cache.clear()
            self.assertGreater(max(media_peaks.waveform(src)), 0.05, 'BRAW sound comes from the container')
            info = media_proxy.describe(src)
            self.assertFalse(info['rawDecoderAvailable'])
            stand_in = self.tmp / 'proxy.mov'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=0xC04020:s=128x96:r=24:d=0.5',
                            '-c:v', 'prores_ks', str(stand_in)], check=True, stdin=subprocess.DEVNULL)
            proxy_src = media_links.link_many([str(stand_in)], validate=media_proxy.decodable)[0]['src']
            project = {'name': 'Attached', 'width': 128, 'height': 96, 'fps': 24,
                       'media': [{**info, 'id': 'm1', 'name': braw.name, 'src': src, 'rawAttachedProxy': proxy_src}],
                       'clips': [{'id': 'c1', 'type': 'video', 'track': 1, 'mediaId': 'm1', 'start': 0, 'in': 0,
                                  'duration': 0.5}]}
            out = self.tmp / 'attached.mp4'
            render_movie.render_to_file(project, out, options={'codec': 'h264', 'limiter': False})
            _, _, rgb = _extract_rgb_frame(out, frame_index=3)
            self.assertGreater(rgb[(48 * 128 + 64) * 3], 150, 'the attached proxy is the exported picture')
            del project['media'][0]['rawAttachedProxy']
            with self.assertRaises(ValueError) as ctx:
                render_movie.render_to_file(project, self.tmp / 'none.mp4', options={'codec': 'h264'})
            self.assertIn('RAW Decoders', str(ctx.exception))


class TestRawServerSecurityAndValidation(_IsolatedDataDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self.httpd = server.create_server(self.port)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = f'{self.httpd.cookie_name}={self.httpd.session}'
        self.token = self.httpd.page_token

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def _request(self, method: str, path: str, body=None, *, auth=True, extra_headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=20)
        headers = {'Host': f'127.0.0.1:{self.port}'}
        if auth:
            headers['Cookie'] = self.cookie
            headers[server.TOKEN_HEADER] = self.token
            headers['Origin'] = f'http://127.0.0.1:{self.port}'
            headers['Sec-Fetch-Site'] = 'same-origin'
        if extra_headers:
            headers.update(extra_headers)
        payload = None
        if body is not None:
            payload = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {}
        return resp.status, parsed

    def test_raw_endpoints_require_auth_and_reject_cross_site(self):
        for method, path, body in (
            ('GET', '/api/raw/decoders', None),
            ('POST', '/api/raw/settings', {'paths': {}}),
            ('POST', '/api/raw/preview', {'src': '/media/deadbeef'}),
        ):
            status, _ = self._request(method, path, body, auth=False)
            self.assertEqual(status, 403, f'{method} {path} must reject unauthenticated requests')
            status_xs, _ = self._request(method, path, body, auth=True, extra_headers={'Origin': 'http://evil.example'})
            self.assertEqual(status_xs, 403, f'{method} {path} must reject cross-origin requests')

        status, data = self._request('GET', '/api/raw/decoders')
        self.assertEqual(status, 200)
        by_id = {d['id']: d for d in data['decoders']}
        self.assertTrue(by_id['cinemadng']['available'])
        self.assertTrue(by_id['prores_raw']['available'])
        for expected in ('prores_raw', 'cinemadng', 'libraw', 'canon_crm', 'braw', 'r3d', 'arriraw', 'nraw', 'xocn'):
            self.assertIn(expected, by_id)

    def test_raw_settings_rejects_traversal_metachars_and_non_executables(self):
        non_exec = self.tmp / 'not_executable.txt'
        non_exec.write_text('hello', encoding='utf-8')
        non_exec.chmod(0o644)

        bad_paths = [
            'relative/path/decoder',
            '/usr/bin/../bin/sh',
            '/tmp/dec;id',
            '/tmp/dec|cat',
            '/tmp/dec$(whoami)',
            '/tmp/dec`id`',
            '/tmp/dec\n/bin/sh',
            str(self.tmp),       # directory
            str(non_exec),       # non-executable regular file
            '/nonexistent/definitely/missing/decoder',
        ]
        for bad in bad_paths:
            status, payload = self._request('POST', '/api/raw/settings', {'paths': {'braw': bad}})
            self.assertIn(status, (400, 422), f'Bad path {bad!r} should be rejected, got {status}: {payload}')
            self.assertIn('error', payload)

        # Unknown decoder id rejected
        status, _ = self._request('POST', '/api/raw/settings', {'paths': {'unknown_backend': '/bin/true'}})
        self.assertIn(status, (400, 422))

        # Valid executable accepted and persisted; empty string clears it
        mock_cli = raw_fixtures.write_mock_raw_cli(self.tmp / 'valid-braw-cli')
        status, data = self._request('POST', '/api/raw/settings', {'paths': {'braw': str(mock_cli)}})
        self.assertEqual(status, 200)
        braw_row = next(d for d in data['decoders'] if d['id'] == 'braw')
        self.assertTrue(braw_row['available'])

        status, data = self._request('POST', '/api/raw/settings', {'paths': {'braw': ''}})
        self.assertEqual(status, 200)
        braw_row = next(d for d in data['decoders'] if d['id'] == 'braw')
        self.assertFalse(braw_row['configuredPath'])

    def test_malformed_and_truncated_raw_headers_fail_safely(self):
        cases = {
            'empty.dng': b'',
            'trunc.dng': b'II*\x00\x08\x00',
            'badmagic.dng': b'NOT_TIFF_HEADER_DATA',
            'cyclic_ifd.dng': b'II*\x00\x08\x00\x00\x00' + struct.pack('<HI', 0, 8),
            'huge_dims.dng': (
                b'II*\x00\x08\x00\x00\x00' + struct.pack('<H', 2)
                + struct.pack('<HHII', 256, 4, 1, 999999)
                + struct.pack('<HHII', 257, 4, 1, 999999)
                + struct.pack('<I', 0)
            ),
            'corrupt.braw': b'\x00' * 1024,
            'trunc.r3d': b'\x00\x00\x00\x10RED1',
            'corrupt.r3d': b'NOT_RED_FILE_DATA',
            'corrupt.ari': b'NOT_ARRI_DATA_AT_ALL',
            'corrupt.crm': b'GARBAGE',
            'corrupt.nev': b'GARBAGE',
        }
        for name, content in cases.items():
            p = self.tmp / name
            p.write_bytes(content)
            with self.assertRaises((raw_bayer.RawError, ValueError), msg=f'{name} must fail cleanly'):
                raw_decode.probe_raw(p)


class TestRawInterchangeAndResolveDisclosure(_IsolatedDataDirMixin, unittest.TestCase):
    def test_interchange_preserves_raw_paths_reel_tc_and_discloses_overrides(self):
        seq_dir = self.tmp / 'interchange_dng'
        first = raw_fixtures.write_cinemadng_sequence(
            seq_dir, prefix='A001_', frames=12, fps=24, bit_depth=12,
            width=96, height=64, with_wav=True, reel='A001', timecode=(1, 2, 3, 4),
        )
        braw = raw_fixtures.write_synthetic_braw(
            self.tmp / 'B002_C001.braw', width=160, height=90, fps=24, duration=0.5,
            timecode='02:00:00:00', reel='B002', with_audio=True,
        )
        linked = media_links.link_many([str(first), str(braw)], validate=media_proxy.decodable)
        project = {
            'name': 'RAW Turnover',
            'width': 1920, 'height': 1080, 'fps': 24,
            'media': [
                {'id': 'm1', 'name': first.name, 'src': linked[0]['src'], 'type': 'video', 'duration': 0.5, 'raw': True},
                {'id': 'm2', 'name': braw.name, 'src': linked[1]['src'], 'type': 'video', 'duration': 0.5, 'raw': True},
            ],
            'clips': [
                {
                    'id': 'c1', 'type': 'video', 'track': 1, 'mediaId': 'm1',
                    'start': 0.0, 'in': 0.0, 'duration': 0.5,
                    'rawSettings': {'decodeUsing': 'clip', 'iso': 1600, 'exposure': 1.0, 'kelvin': 7200},
                },
                {
                    'id': 'c2', 'type': 'video', 'track': 1, 'mediaId': 'm2',
                    'start': 0.5, 'in': 0.0, 'duration': 0.5,
                    'rawSettings': {'decodeUsing': 'camera'},
                },
            ],
        }
        cut = build_cut(project)
        self.assertEqual(cut.sources['m1'].path, str(first.resolve()))
        self.assertEqual(cut.sources['m1'].reel, 'A001')
        self.assertEqual(cut.sources['m1'].timecode, '01:02:03:04')
        self.assertEqual(cut.sources['m2'].path, str(braw.resolve()))
        self.assertEqual(cut.sources['m2'].reel, 'B002')
        self.assertEqual(cut.sources['m2'].timecode, '02:00:00:00')
        # Clip c1 has RAW overrides disclosed; c2 uses camera metadata so no RAW override warning
        raw_notes = [n for n in cut.report.general if 'Camera RAW decode overrides' in n]
        self.assertEqual(len(raw_notes), 1)
        self.assertIn('ISO 1600', raw_notes[0])
        self.assertIn('7200K', raw_notes[0])

        # Export all 4 interchange formats cleanly
        for fmt in ('fcpxml', 'xmeml', 'edl', 'otio'):
            exported = interchange_handle('export', {'project': project, 'format': fmt})
            self.assertTrue(exported['files'], f'No files returned for {fmt}')
            text = '\n'.join(f['content'] for f in exported['files'])
            self.assertTrue(len(text) > 50)
            if fmt != 'edl':
                self.assertIn(first.name, text)
                self.assertIn(braw.name, text)
            else:
                self.assertIn('A001', text)
                self.assertIn('B002', text)

    def test_resolve_writer_reports_raw_decode_overrides(self):
        from resolve import writer as rw
        self.assertEqual(rw.CLIP_FIELDS.get('rawSettings'), 'reported')
        self.assertIn('raw', rw.MEDIA_FIELDS)
        # Unit-check _not_written disclosure directly on a minimal Writer stub
        w = object.__new__(rw.Writer)
        w.report = rw.Report()
        w.base_clips = {}
        w.current_media = {}
        clip_override = {'id': 'c1', 'type': 'video', 'track': 1, 'rawSettings': {'decodeUsing': 'clip', 'exposure': 1.5}}
        w._not_written(clip_override, 'video', None, 'Timeline · A001')
        lines = [line['what'] for line in w.report.lines()]
        self.assertTrue(any('Camera RAW decode overrides' in line for line in lines), f'Expected RAW disclosure in {lines}')

        w2 = object.__new__(rw.Writer)
        w2.report = rw.Report()
        w2.base_clips = {}
        w2.current_media = {}
        clip_cam = {'id': 'c2', 'type': 'video', 'track': 1, 'rawSettings': {'decodeUsing': 'camera'}}
        w2._not_written(clip_cam, 'video', None, 'Timeline · A001')
        lines2 = [line['what'] for line in w2.report.lines()]
        self.assertFalse(any('Camera RAW' in line for line in lines2))


if __name__ == '__main__':
    unittest.main()
