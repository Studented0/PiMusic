/*
 * PiMusic Rotary Encoder — Pro Micro (ATmega32U4) USB HID Keyboard
 *
 * Reads an E11 rotary encoder (A/B on pins 2/3) and its push-button
 * (pin 4), then sends USB keyboard events that the PiMusic web UI
 * consumes via a global keydown listener.
 *
 *   CW rotation  -> KEY_DOWN_ARROW (volume down)
 *   CCW rotation -> KEY_UP_ARROW   (volume up)
 *   Button press -> Space          (play / pause)
 *
 * Wiring (all use internal pull-ups, no external resistors needed):
 *   Encoder A  -> Pin 2
 *   Encoder B  -> Pin 3
 *   Button     -> Pin 4
 *   Encoder/Button common -> GND
 *
 * Board: select "Arduino Leonardo" or "SparkFun Pro Micro" in the IDE.
 */

#include <Keyboard.h>

#define PIN_A       2
#define PIN_B       3
#define PIN_BTN     4
#define DEBOUNCE_MS 50

static uint8_t       lastAB;
static bool          lastBtn  = HIGH;
static unsigned long btnTime  = 0;

/*
 * Gray-code direction lookup table.
 * Index = (previousAB << 2) | currentAB  (4-bit, 16 entries)
 *
 *  CW transitions:  00->01  01->11  11->10  10->00   => +1
 *  CCW transitions: 00->10  10->11  11->01  01->00   => -1
 *  Same / invalid:  0
 */
static const int8_t DIR_TABLE[16] = {
   0,  1, -1,  0,
  -1,  0,  0,  1,
   1,  0,  0, -1,
   0, -1,  1,  0
};

void setup() {
  pinMode(PIN_A,   INPUT_PULLUP);
  pinMode(PIN_B,   INPUT_PULLUP);
  pinMode(PIN_BTN, INPUT_PULLUP);

  lastAB = (digitalRead(PIN_A) << 1) | digitalRead(PIN_B);

  Keyboard.begin();
}

void loop() {
  /* ── Encoder rotation ──────────────────────────────── */
  uint8_t ab = (digitalRead(PIN_A) << 1) | digitalRead(PIN_B);

  if (ab != lastAB) {
    int8_t dir = DIR_TABLE[(lastAB << 2) | ab];

    if (dir == 1) {
      Keyboard.press(KEY_DOWN_ARROW);
      Keyboard.release(KEY_DOWN_ARROW);
    } else if (dir == -1) {
      Keyboard.press(KEY_UP_ARROW);
      Keyboard.release(KEY_UP_ARROW);
    }

    lastAB = ab;
  }

  /* ── Push-button (active LOW, debounced) ───────────── */
  bool btn = digitalRead(PIN_BTN);

  if (btn == LOW && lastBtn == HIGH && (millis() - btnTime) > DEBOUNCE_MS) {
    Keyboard.press(' ');
    Keyboard.release(' ');
    btnTime = millis();
  }

  lastBtn = btn;

  delayMicroseconds(500);   // ~2 kHz poll rate
}
