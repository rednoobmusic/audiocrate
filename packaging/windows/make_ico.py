"""audiocrate.ico from the program's icon.png: python3 make_ico.py icon.png audiocrate.ico"""
import sys

from PIL import Image

src = Image.open(sys.argv[1]).convert("RGBA")
sizes = [(n, n) for n in (16, 24, 32, 40, 48, 64, 96, 128, 256) if n <= min(src.size)]
src.save(sys.argv[2], sizes=sizes)
print(f"{sys.argv[2]}: {', '.join(str(w) for w, _ in sizes)} px")
