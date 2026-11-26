"""Validate and serialize bounded 3D and 1D LUTs for FFmpeg."""
import math

# Keep in step with lut.js: 65 points per axis is what colorists commonly deliver.
MAX_SIZE = 65
# FFmpeg's lut1d reads up to 65536 entries per channel.
MAX_SIZE_1D = 65536


def cube_text(lut, intensity=100, *, normalized_domain=False):
    if not isinstance(lut, dict):
        raise ValueError('Invalid LUT')
    size = lut.get('size')
    data = lut.get('data')
    if isinstance(size, bool) or not isinstance(size, int) or not 2 <= size <= MAX_SIZE or not isinstance(data, list) or len(data) != size**3*3:
        raise ValueError('Invalid 3D LUT shape')
    if not all(_sample(value) for value in data):
        raise ValueError('Invalid 3D LUT sample')
    domain_min, domain_max, alpha = _domain_and_mix(lut, intensity)
    if normalized_domain:
        domain_min, domain_max = [0,0,0], [1,1,1]
    lines = [f'LUT_3D_SIZE {size}', 'DOMAIN_MIN '+ ' '.join(str(v) for v in domain_min),
             'DOMAIN_MAX '+ ' '.join(str(v) for v in domain_max)]
    for blue in range(size):
        for green in range(size):
            for red in range(size):
                i=3*(red+size*(green+size*blue))
                identity=(red/(size-1),green/(size-1),blue/(size-1))
                lines.append(' '.join(f'{identity[c]*(1-alpha)+data[i+c]*alpha:.8f}' for c in range(3)))
    return '\n'.join(lines)+'\n'


def cube1d_text(lut, intensity=100, *, normalized_domain=False):
    """The full-resolution 1D curve a 1D .cube file carried (`lut['curve']`), for FFmpeg's lut1d.

    lut.js keeps a 1D file both as a 3D lattice of at most 65 points (`size`/`data`, which
    cube_text and lut3d apply today) and as this curve. With
    `lut1d=file=...:interp=linear` in place of lut3d, the export applies the curve exactly.
    """
    curve = lut.get('curve') if isinstance(lut, dict) else None
    if not isinstance(curve, dict):
        raise ValueError('This LUT has no 1D curve')
    size, data = curve.get('size'), curve.get('data')
    if isinstance(size, bool) or not isinstance(size, int) or not 2 <= size <= MAX_SIZE_1D or not isinstance(data, list) or len(data) != size*3:
        raise ValueError('Invalid 1D LUT shape')
    if not all(_sample(value) for value in data):
        raise ValueError('Invalid 1D LUT sample')
    domain_min, domain_max, alpha = _domain_and_mix(lut, intensity)
    if normalized_domain:
        domain_min, domain_max = [0,0,0], [1,1,1]
    lines = [f'LUT_1D_SIZE {size}', 'DOMAIN_MIN '+ ' '.join(str(v) for v in domain_min),
             'DOMAIN_MAX '+ ' '.join(str(v) for v in domain_max)]
    for index in range(size):
        identity = index/(size-1)
        lines.append(' '.join(f'{identity*(1-alpha)+data[3*index+c]*alpha:.8f}' for c in range(3)))
    return '\n'.join(lines)+'\n'


def _sample(value):
    return not isinstance(value, bool) and isinstance(value, (float, int)) and math.isfinite(value) and -4 <= value <= 4


def _domain_and_mix(lut, intensity):
    domain_min = lut.get('domainMin', [0,0,0])
    domain_max = lut.get('domainMax', [1,1,1])
    if (not isinstance(domain_min, list) or not isinstance(domain_max, list) or
        len(domain_min)!=3 or len(domain_max)!=3 or any(
            isinstance(a, bool) or isinstance(b, bool) or
            not isinstance(a,(float,int)) or not isinstance(b,(float,int)) or
            not math.isfinite(a) or not math.isfinite(b) or a>=b or abs(a)>16 or abs(b)>16
            for a,b in zip(domain_min,domain_max))):
        raise ValueError('Invalid 3D LUT domain')
    if isinstance(intensity, bool) or not isinstance(intensity, (float,int)):
        raise ValueError('Invalid LUT intensity')
    alpha = float(intensity)/100
    if not math.isfinite(alpha) or not 0 <= alpha <= 1:
        raise ValueError('Invalid LUT intensity')
    return domain_min, domain_max, alpha
