"""Validate and serialize bounded 3D LUTs for FFmpeg."""
import math


def cube_text(lut, intensity=100):
    if not isinstance(lut, dict):
        raise ValueError('Invalid LUT')
    size = lut.get('size')
    data = lut.get('data')
    if not isinstance(size, int) or not 2 <= size <= 33 or not isinstance(data, list) or len(data) != size**3*3:
        raise ValueError('Invalid 3D LUT shape')
    if any(not isinstance(value,(float,int)) or not math.isfinite(value) or not 0 <= value <= 1 for value in data):
        raise ValueError('Invalid 3D LUT sample')
    domain_min = lut.get('domainMin', [0,0,0])
    domain_max = lut.get('domainMax', [1,1,1])
    if len(domain_min)!=3 or len(domain_max)!=3 or any(not math.isfinite(a) or not math.isfinite(b) or a>=b for a,b in zip(domain_min,domain_max)):
        raise ValueError('Invalid 3D LUT domain')
    alpha = float(intensity)/100
    if not math.isfinite(alpha) or not 0 <= alpha <= 1:
        raise ValueError('Invalid LUT intensity')
    lines = [f'LUT_3D_SIZE {size}', 'DOMAIN_MIN '+ ' '.join(str(v) for v in domain_min),
             'DOMAIN_MAX '+ ' '.join(str(v) for v in domain_max)]
    for blue in range(size):
        for green in range(size):
            for red in range(size):
                i=3*(red+size*(green+size*blue))
                identity=(red/(size-1),green/(size-1),blue/(size-1))
                lines.append(' '.join(f'{identity[c]*(1-alpha)+data[i+c]*alpha:.8f}' for c in range(3)))
    return '\n'.join(lines)+'\n'
