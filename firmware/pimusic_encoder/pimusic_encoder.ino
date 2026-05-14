#include <TFT_eSPI.h>

TFT_eSPI tft = TFT_eSPI();

void setup() {
  pinMode(19, OUTPUT);
  digitalWrite(19, HIGH);
  
  tft.init();
  tft.setRotation(0);
  tft.fillScreen(TFT_RED);
  delay(1000);
  tft.fillScreen(TFT_GREEN);
  delay(1000);
  tft.fillScreen(TFT_BLUE);
  delay(1000);
  
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.setTextSize(3);
  tft.fillScreen(TFT_BLACK);
  tft.setCursor(20, 100);
  tft.println("DISPLAY");
  tft.setCursor(20, 140);
  tft.println("WORKING");
}

void loop() {}