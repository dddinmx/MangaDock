#!/usr/bin/env python3
import sys,json
from PIL import Image,ImageChops
reference=Image.open(sys.argv[1]).convert('RGB');actual=Image.open(sys.argv[2]).convert('RGB')
if reference.size!=actual.size:raise SystemExit('Screenshot dimensions differ')
difference=ImageChops.difference(reference,actual)
# Tiny antialiasing/animation differences are tolerated; layout, labels, colors and missing blocks are not.
pixels=list(difference.getdata());changed=sum(max(pixel)>20 for pixel in pixels);ratio=changed/len(pixels)
print(json.dumps({'reference':sys.argv[1],'changed_pixel_ratio':ratio}))
if ratio>.002:difference.save(sys.argv[2].replace('-actual.png','-diff.png'));raise SystemExit(1)
