"""Load random artwork at timed interval- Broken by Tom Nardi"""

from apps.base_app import BaseApp
from lib.button import MenuMinus, MenuPlus, UserA, UserB, Boot
from lib.epd import display
from lib.led_bar import NUM_LEDS, leds

import time
import random

class GalleryApp(BaseApp):
    def __init__(self, name: str | None = None) -> None:
        self.name = "Gallery"
        self.sleep_ms = 100
        self.launched = False
        
        # Number of images
        self.maximage = 44
        
        # Time between image updates
        self.updatetime = 300
   
        # Variable to hold time since last change
        self.lastupdate = 0
    
    def random_art(self):
        leds.fill((0, 1, 1))
        display.clear()
        display.draw_png(f'/data/gallery/' + "{:03d}".format(random.randint(1, self.maximage)) + '.png')
        display.update()
        leds.fill((0, 1, 0))
    
    def run(self):
        if self.launched or UserA.pressed() or UserB.pressed():
            self.random_art()
            self.lastupdate = time.time()
        elif time.time() > self.lastupdate + self.updatetime:
            self.random_art()
            self.lastupdate = time.time()
