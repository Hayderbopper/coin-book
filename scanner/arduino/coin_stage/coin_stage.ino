// Coin scanner XY stage firmware
// Board: Arduino Nano (ATmega328P, CH340). Motors: 2x 28BYJ-48 5V with ULN2003 boards.
//
// Wiring (ULN2003 IN1..IN4):
//   X axis: D8, D9, D10, D11
//   Y axis: D4, D5, D6, D7
//   Both driver boards: + to Nano 5V, - to Nano GND
//
// Serial commands (115200 baud, one per line, answers "OK ..." or "ERR ..."):
//   MOVE x y    move to absolute position in mm
//   JOG dx dy   move relative to the current position in mm
//   HOME        call the current position 0,0
//   POS         report the current position
//   OFF         switch the motor coils off
//   PING        answers "OK coin_stage"

// ---- Settings you may need to change ----
const float LEAD_MM = 8.0;            // lead screw travel per turn: 8 for "lead 8mm", 2 for "lead 2mm"
const float STEPS_PER_REV = 4096.0;   // 28BYJ-48 in half-step mode
const float BACKLASH_MM = 0.3;        // overshoot so every move ends travelling the same way
const float LIMIT_MM = 24.0;          // soft limit either side of HOME
const unsigned int STEP_US = 1200;    // time per half-step; raise it if a motor skips
const bool INVERT_X = false;          // flip if X moves the wrong way
const bool INVERT_Y = false;          // flip if Y moves the wrong way

const float STEPS_PER_MM = STEPS_PER_REV / LEAD_MM;

const byte HALF_STEP[8][4] = {
  {1, 0, 0, 0}, {1, 1, 0, 0}, {0, 1, 0, 0}, {0, 1, 1, 0},
  {0, 0, 1, 0}, {0, 0, 1, 1}, {0, 0, 0, 1}, {1, 0, 0, 1}
};

struct Axis {
  byte pins[4];
  bool invert;
  long pos;     // in half-steps
  byte phase;   // 0..7
};

Axis axisX = {{8, 9, 10, 11}, INVERT_X, 0, 0};
Axis axisY = {{4, 5, 6, 7}, INVERT_Y, 0, 0};

void writePhase(Axis &a) {
  for (byte i = 0; i < 4; i++) {
    digitalWrite(a.pins[i], HALF_STEP[a.phase][i]);
  }
}

void coilsOff(Axis &a) {
  for (byte i = 0; i < 4; i++) {
    digitalWrite(a.pins[i], LOW);
  }
}

void stepTo(Axis &a, long target) {
  while (a.pos != target) {
    int dir = (target > a.pos) ? 1 : -1;
    int phaseDir = a.invert ? -dir : dir;
    a.phase = (a.phase + 8 + phaseDir) % 8;
    writePhase(a);
    a.pos += dir;
    delayMicroseconds(STEP_US);
  }
}

// Moves one axis at a time (keeps current draw low enough for USB power).
// Moves that go negative overshoot by BACKLASH_MM and come back, so the
// gear play is always taken up in the same direction.
void moveAxis(Axis &a, float mm) {
  long target = lround(mm * STEPS_PER_MM);
  long backlash = lround(BACKLASH_MM * STEPS_PER_MM);
  writePhase(a);  // re-energise at the stored phase before moving
  if (target < a.pos) {
    stepTo(a, target - backlash);
  }
  stepTo(a, target);
  delay(20);
  coilsOff(a);
}

bool inLimits(float x, float y) {
  return fabs(x) <= LIMIT_MM && fabs(y) <= LIMIT_MM;
}

void moveTo(float x, float y) {
  if (!inLimits(x, y)) {
    Serial.println(F("ERR outside limits"));
    return;
  }
  moveAxis(axisX, x);
  moveAxis(axisY, y);
  reportPos();
}

void reportPos() {
  Serial.print(F("OK "));
  Serial.print(axisX.pos / STEPS_PER_MM, 3);
  Serial.print(' ');
  Serial.println(axisY.pos / STEPS_PER_MM, 3);
}

void handleLine(String line) {
  line.trim();
  line.toUpperCase();
  if (line.length() == 0) return;

  int sp1 = line.indexOf(' ');
  String cmd = (sp1 < 0) ? line : line.substring(0, sp1);
  String args = (sp1 < 0) ? "" : line.substring(sp1 + 1);
  args.trim();
  int sp2 = args.indexOf(' ');
  float a1 = (sp2 < 0) ? args.toFloat() : args.substring(0, sp2).toFloat();
  float a2 = (sp2 < 0) ? 0 : args.substring(sp2 + 1).toFloat();

  if (cmd == "MOVE") {
    moveTo(a1, a2);
  } else if (cmd == "JOG") {
    moveTo(axisX.pos / STEPS_PER_MM + a1, axisY.pos / STEPS_PER_MM + a2);
  } else if (cmd == "HOME") {
    axisX.pos = 0;
    axisY.pos = 0;
    reportPos();
  } else if (cmd == "POS") {
    reportPos();
  } else if (cmd == "OFF") {
    coilsOff(axisX);
    coilsOff(axisY);
    Serial.println(F("OK off"));
  } else if (cmd == "PING") {
    Serial.println(F("OK coin_stage"));
  } else {
    Serial.println(F("ERR unknown command"));
  }
}

void setup() {
  for (byte i = 0; i < 4; i++) {
    pinMode(axisX.pins[i], OUTPUT);
    pinMode(axisY.pins[i], OUTPUT);
  }
  coilsOff(axisX);
  coilsOff(axisY);
  Serial.begin(115200);
  Serial.println(F("OK coin_stage ready"));
}

void loop() {
  static String buffer;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      handleLine(buffer);
      buffer = "";
    } else if (buffer.length() < 40) {
      buffer += c;
    }
  }
}
