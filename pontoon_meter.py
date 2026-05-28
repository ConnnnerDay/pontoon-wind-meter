from luma.core.interface.serial import spi
from luma.lcd.device import ili9341
from PIL import Image, ImageDraw
from urllib.request import urlopen
import time, math

URL="https://www.ndbc.noaa.gov/data/realtime2/41038.txt"

serial=spi(port=0,device=0,gpio_DC=24,gpio_RST=25)
device=ili9341(serial,width=320,height=240,rotate=1)

def mph(k): return k*1.15078

def draw_needle(d,cx,cy,r,val):
    pct=min(max(val/25,0),1)
    ang=math.radians(180-(pct*180))
    x=cx+r*math.cos(ang)
    y=cy-r*math.sin(ang)
    d.line((cx,cy,x,y),fill="white",width=4)
    d.ellipse((cx-6,cy-6,cx+6,cy+6),fill="white")

while True:
    try:
        txt=urlopen(URL,timeout=10).read().decode()
        lines=[x.split() for x in txt.splitlines() if x.strip()]
        row=dict(zip(lines[0],lines[2]))

        wind=float(row["WSPD"])
        gust=float(row["GST"]) if row["GST"]!="MM" else wind
        w=mph(wind)
        g=mph(gust)

        if g < 12:
            msg="GOOD"
            msgc="green"
        elif g <= 18:
            msg="CAUTION"
            msgc="yellow"
        else:
            msg="TOO WINDY"
            msgc="red"

        img=Image.new("RGB",device.size,"black")
        d=ImageDraw.Draw(img)

        cx,cy,r=160,205,112
        box=(cx-r,cy-r,cx+r,cy+r)

        d.text((78,8),"PONTOON WIND",fill="white")
        d.text((105,30),msg,fill=msgc)
        d.text((80,52),f"Gust {g:.1f} mph",fill="white")
        d.text((85,70),f"Wind {w:.1f} mph",fill="white")

        d.arc(box,180,240,fill="green",width=16)
        d.arc(box,240,300,fill="yellow",width=16)
        d.arc(box,300,360,fill="red",width=16)

        draw_needle(d,cx,cy,82,g)

        d.text((28,200),"0",fill="white")
        d.text((147,92),"12",fill="white")
        d.text((275,200),"25",fill="white")

        device.display(img)

    except Exception as e:
        img=Image.new("RGB",device.size,"black")
        d=ImageDraw.Draw(img)
        d.text((20,20),"ERROR",fill="red")
        d.text((20,60),str(e)[:30],fill="white")
        device.display(img)

    time.sleep(300)
