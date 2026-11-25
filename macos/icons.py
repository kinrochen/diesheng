"""Generate the app and menu-bar icons from the product's two speech bubbles."""
from pathlib import Path
import subprocess
import tempfile
from PIL import Image, ImageDraw


def bubble(size, box, color, angle):
    layer = Image.new('RGBA', (size, size))
    draw = ImageDraw.Draw(layer)
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=int(size * .105), fill=color)
    draw.polygon([(x1, y2 - size*.12), (x1, y2 + size*.035), (x1 + size*.16, y2 - size*.025)], fill=color)
    return layer.rotate(angle, Image.Resampling.BICUBIC)


def render(size=1024, status=False):
    image = Image.new('RGBA', (size, size))
    if not status:
        ImageDraw.Draw(image).rounded_rectangle((size*.055, size*.055, size*.945, size*.945), radius=size*.22, fill='#35152d')
    rear = '#ffffff' if status else '#b39cbf'
    front = '#ffffff' if status else '#f7cfa9'
    cut = (0, 0, 0, 0) if status else '#35152d'
    image.alpha_composite(bubble(size, (size*.34, size*.20, size*.79, size*.57), rear, -10))
    mask = bubble(size, (size*.16, size*.39, size*.70, size*.76), '#ffffff', 8).getchannel('A')
    if status:
        from PIL import ImageFilter
        image.putalpha(Image.composite(Image.new('L', (size,size), 0), image.getchannel('A'), mask.filter(ImageFilter.MaxFilter(45 if size > 100 else 3))))
    else:
        image.alpha_composite(bubble(size, (size*.145, size*.375, size*.715, size*.775), cut, 8))
    image.alpha_composite(bubble(size, (size*.16, size*.39, size*.70, size*.76), front, 8))
    dots = Image.new('RGBA', (size,size)); d = ImageDraw.Draw(dots)
    for x in (.29,.41,.53): d.ellipse((size*x-size*.024,size*.565-size*.024,size*x+size*.024,size*.565+size*.024), fill='#35152d' if not status else '#000000')
    dots = dots.rotate(8, Image.Resampling.BICUBIC)
    if status:
        alpha = image.getchannel('A'); alpha.paste(0, (0,0), dots.getchannel('A')); image.putalpha(alpha)
    else: image.alpha_composite(dots)
    return image


def create(resources):
    resources = Path(resources); resources.mkdir(parents=True, exist_ok=True)
    render(status=True).resize((80,80), Image.Resampling.LANCZOS).save(resources / 'status.png')
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / 'AppIcon.iconset'; iconset.mkdir()
        original = render()
        for size in (16,32,128,256,512):
            for scale in (1,2):
                original.resize((size*scale,size*scale),Image.Resampling.LANCZOS).save(iconset / f'icon_{size}x{size}{"@2x" if scale == 2 else ""}.png')
        subprocess.run(['iconutil','-c','icns',str(iconset),'-o',str(resources / 'AppIcon.icns')],check=True)
