"""Rasterize Japanese reply and settings fonts with 16-level antialiasing."""
from pathlib import Path
from PIL import Image, ImageFont


def rasterize(chars, size, name, path, font_path, write_changed):
    font = ImageFont.truetype(str(font_path), size)
    data = bytearray()
    desc = ['{0}']
    for c in sorted(chars):
        mask, (left, top) = font.getmask2(c, mode='L', anchor='ls')
        w, h = mask.size
        pixels = []
        if w and h:
            pic = Image.frombytes('L', mask.size, bytes(mask))
            box = pic.getbbox()
            if box:
                pic = pic.crop(box); w, h = pic.size
                left += box[0]; top += box[1]
                pixels = [(v * 15 + 127) // 255 for v in pic.get_flattened_data()]
            else: w = h = 0
        else: w = h = 0
        desc.append(f'{{.bitmap_index={len(data)},.adv_w={round(font.getlength(c)*16)},.box_w={w},.box_h={h},.ofs_x={left},.ofs_y={-top-h}}}')
        if len(pixels) % 2: pixels.append(0)
        data.extend((pixels[i] << 4) | pixels[i + 1] for i in range(0, len(pixels), 2))
    offsets = ','.join(str(ord(c) - 32) for c in sorted(chars))
    source = ['/* Noto Sans CJK JP 2.004 subset, SIL OFL 1.1. See THIRD_PARTY_NOTICES.md. */\n#include "lvgl.h"\n',
              'static const uint8_t bits[]={\n']
    for i in range(0, len(data), 24): source.append(','.join(hex(v) for v in data[i:i+24]) + ',\n')
    source.extend(['};\nstatic const lv_font_fmt_txt_glyph_dsc_t glyphs[]={\n', ',\n'.join(desc),
        f'}};\nstatic const uint16_t codes[]={{{offsets}}};\n',
        'static const lv_font_fmt_txt_cmap_t cmap[]={\n',
        f'{{.range_start=32,.range_length={ord(max(chars))-31},.glyph_id_start=1,.unicode_list=codes,.list_length={len(chars)},.type=LV_FONT_FMT_TXT_CMAP_SPARSE_TINY}}}};\n',
        'static const lv_font_fmt_txt_dsc_t data={.glyph_bitmap=bits,.glyph_dsc=glyphs,.cmaps=cmap,.cmap_num=1,.bpp=4};\n',
        f'const lv_font_t {name}={{.get_glyph_dsc=lv_font_get_glyph_dsc_fmt_txt,.get_glyph_bitmap=lv_font_get_bitmap_fmt_txt,.line_height={size+6},.base_line=6,.dsc=&data}};\n'])
    if len(data) >= 1 << 20:
        raise RuntimeError('Font exceeds LVGL compact bitmap offsets')
    write_changed(path, ''.join(source).encode())
    print(f'{name}: {len(chars)} Noto glyphs, {len(data)} bitmap bytes, 4 bpp')


def generate(esp: Path, overlay: Path, write_changed, font_path: Path):
    labels = set(chr(i) for i in range(32, 127))
    for path in [overlay / 'sumi_ui.c', overlay.parent / 'main/sumi_wifi.c']:
        labels.update(c for c in path.read_text() if 0x3000 <= ord(c) < 0x10000)
    rasterize(labels, 24, 'sumi_font_24', esp / 'components/muse/sumi_font_24.c', font_path, write_changed)
    rasterize(set('0123456789:-'), 64, 'sumi_clock_64', esp / 'components/muse/sumi_clock_64.c', font_path, write_changed)
    # JIS X 0208 includes both levels of Japanese kanji, kana, symbols, Greek
    # and Cyrillic. Replace the larger pan-CJK font rather than keeping two
    # full bitmaps in PSRAM; preserve Latin fallback in sumi_ui.c.
    reply = labels.copy()
    for row in range(0x21, 0x7f):
        for cell in range(0x21, 0x7f):
            try:
                reply.update((b'\x1b$B' + bytes([row, cell]) + b'\x1b(B').decode('iso2022_jp'))
            except UnicodeDecodeError:
                pass
    reply.update(chr(i) for i in range(0x3000, 0x3100))
    reply.update(chr(i) for i in range(0xff01, 0xffa0))
    rasterize(reply, 16, 'muse_font_cjk_16', esp / 'components/muse/fonts/muse_font_cjk_16.c', font_path, write_changed)
