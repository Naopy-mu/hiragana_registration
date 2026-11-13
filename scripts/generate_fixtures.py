"""Generate explicitly synthetic study sheets and perspective phone-style photos."""
from pathlib import Path
import argparse
import json
import sys

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import qrcode

ROOT = Path(__file__).resolve().parents[1]


def generate(destination: Path, manifest: dict | None = None) -> list[Path]:
    manifest = manifest or json.loads((ROOT/"config/pages.test.json").read_text(encoding="utf-8"))
    destination.mkdir(parents=True,exist_ok=True)
    geometry = manifest["geometry"]
    width=1654
    paper_w,paper_h=geometry["paper_mm"]
    height=round(width*paper_h/paper_w)
    density=width/paper_w
    paths=[]
    sheets=[]
    for page in manifest["pages"]:
        sheet=Image.new("RGB",(width,height),"white")
        draw=ImageDraw.Draw(sheet)
        for mx,my in geometry["marker_centers_mm"]:
            cx,cy=round(mx*density),round(my*density)
            radius=round(1.35*density)
            draw.ellipse((cx-radius,cy-radius,cx+radius,cy+radius),fill="black")
        try:
            font=ImageFont.truetype("DejaVuSans.ttf",48)
            small=ImageFont.truetype("DejaVuSans.ttf",25)
        except OSError:
            font=ImageFont.load_default(size=48)
            small=ImageFont.load_default(size=25)
        n=page["page_number"]
        draw.text((210,220),f"SYNTHETIC TEST PAGE {n}",font=font,fill=(40,40,40))
        draw.text((210,295),page["qr_code"],font=small,fill=(65,65,65))
        for row in range(12):
            y=430+row*128
            for col in range(17):
                x=200+col*73
                draw.rectangle((x,y,x+73,y+70),outline=(190,190,190),width=1)
                # Pale gray strokes emulate pencil and remain after correction.
                draw.line((x+17,y+20,x+39,y+46,x+53,y+16),fill=(130+row%3*15,)*3,width=2)
                if col%5==0:
                    draw.ellipse((x+48,y+7,x+51,y+10),fill=(130,130,130))
        # Distinct upper-page bar verifies page order without relying on PDF text.
        draw.rectangle((225,340,225+n*100,370),fill=(50,50,50))
        qr=qrcode.QRCode(version=1,error_correction=qrcode.constants.ERROR_CORRECT_M,box_size=5,border=4)
        qr.add_data(page["qr_code"]);qr.make(fit=True)
        qr_image=qr.make_image(fill_color="black",back_color="white").convert("RGB")
        side=round(14*density)
        qr_image=qr_image.resize((side,side),Image.Resampling.NEAREST)
        sheet.paste(qr_image,(round(105*density-side/2),round(275*density-side/2)))
        sheet.save(destination/f"page-{n:02d}.png")
        sheets.append(sheet)
        # Outer paper bounds are fully visible, with mild perspective and shading.
        canvas=np.full((2700,2000,3),110,np.uint8)
        source=np.array([[0,0],[width-1,0],[width-1,height-1],[0,height-1]],np.float32)
        corners=np.array([[190,150],[1790,195],[1835,2520],[125,2480]],np.float32)
        transform=cv2.getPerspectiveTransform(source,corners)
        camera=cv2.warpPerspective(np.array(sheet),transform,(2000,2700),borderValue=(110,110,110))
        yy,xx=np.mgrid[:2700,:2000]
        shade=.86+.12*xx/2000
        camera=np.clip(camera*shade[:,:,None],0,255).astype(np.uint8)
        path=destination/f"photo-{n:02d}.jpg"
        Image.fromarray(camera).save(path,quality=95)
        paths.append(path)
    sheets[0].save(destination/"test-sheets.pdf",save_all=True,append_images=sheets[1:],resolution=200)
    return paths


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=ROOT/"local-data/fixtures")
    args=parser.parse_args()
    for result in generate(args.output):
        print(result)
