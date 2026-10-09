/*
 * ESP32-S3 Sensor Streaming
 *
 * Sensors:
 *   MAX30102 -> IR, RED
 *   MPU6500  -> Acceleration X/Y/Z + Magnitude
 *   Grove GSR v2.0 -> Raw ADC
 *
 * Output JSON:
 *   {"ir":...,"red":...,"accMagnitude":...,"eda":...}
 *
 * Target system rate:
 *   64 Hz
 *
 * I2C:
 *   SDA = GPIO 8
 *   SCL = GPIO 9
 */

#include <Wire.h>
#include <MAX30105.h>
#include <MPU6500_WE.h>
#include <ArduinoJson.h>
#include <math.h>


// ============================================================
// PINS
// ============================================================

#define SDA_PIN 8
#define SCL_PIN 9
#define GSR_PIN 13


// ============================================================
// SENSORS
// ============================================================

MAX30105 particleSensor;

MPU6500_WE myMPU6500 = MPU6500_WE(0x68);


// ============================================================
// JSON
// ============================================================

StaticJsonDocument<128> doc;


// ============================================================
// SAMPLE TIMING
// ============================================================

const uint32_t SAMPLE_INTERVAL_US = 15625UL;  // 64 Hz

uint32_t nextSampleTime = 0;


// ============================================================
// LAST VALID MAX30102 VALUES
// ============================================================

uint32_t lastIR = 0;
uint32_t lastRed = 0;


// ============================================================
// RATE MONITOR
// ============================================================

uint32_t rateCounter = 0;
uint32_t rateStart = 0;


// ============================================================
// SETUP
// ============================================================

void setup()
{
    // --------------------------------------------------------
    // Serial
    // --------------------------------------------------------

    Serial.begin(115200);

    delay(500);


    // --------------------------------------------------------
    // I2C
    // --------------------------------------------------------

    Wire.begin(
        SDA_PIN,
        SCL_PIN
    );

    Wire.setClock(400000);


    // --------------------------------------------------------
    // GSR ADC
    // --------------------------------------------------------

    analogReadResolution(12);

    analogSetPinAttenuation(
        GSR_PIN,
        ADC_11db
    );


    // ========================================================
    // MAX30102
    // ========================================================

    if (!particleSensor.begin(
            Wire,
            I2C_SPEED_FAST
        ))
    {
        Serial.println(
            "MAX30102 Initialization Failed"
        );

        while (1)
        {
            delay(1000);
        }
    }


    /*
     * MAX30102 configuration
     *
     * We use a higher internal sensor rate so that the
     * ESP32 always has samples available.
     */

    particleSensor.setup(
        0x1F,   // LED brightness
        4,      // averaging
        2,      // Red + IR
        400,    // sensor sample rate
        411,    // pulse width
        4096    // ADC range
    );


    particleSensor.setPulseAmplitudeRed(
        0x1F
    );

    particleSensor.setPulseAmplitudeIR(
        0x1F
    );


    // ========================================================
    // MPU6500
    // ========================================================

    if (!myMPU6500.init())
    {
        Serial.println(
            "MPU6500 Initialization Failed"
        );

        while (1)
        {
            delay(1000);
        }
    }


    delay(100);


    myMPU6500.autoOffsets();


    myMPU6500.setAccRange(
        MPU6500_ACC_RANGE_2G
    );


    myMPU6500.setAccDLPF(
        MPU6500_DLPF_6
    );


    /*
     * Keep MPU6500 internal sampling reasonably fast.
     * The ESP32 controls the final 64-Hz output rate.
     */
    myMPU6500.setSampleRateDivider(4);


    // ========================================================
    // START
    // ========================================================

    Serial.println(
        "Sensors Ready"
    );


    // Give MAX30102 FIFO a little time to fill
    delay(100);


    nextSampleTime = micros();

    rateStart = millis();
}


// ============================================================
// LOOP
// ============================================================

void loop()
{
    uint32_t now = micros();


    // --------------------------------------------------------
    // 64-Hz scheduler
    // --------------------------------------------------------

    if ((int32_t)(now - nextSampleTime) < 0)
    {
        return;
    }


    /*
     * Advance the schedule immediately.
     *
     * This avoids accumulating timing delays.
     */
    nextSampleTime += SAMPLE_INTERVAL_US;


    // ========================================================
    // MAX30102
    // ========================================================

    /*
     * Check the MAX30102 FIFO once.
     *
     * We do NOT call getIR() and getRed() separately because
     * those operations can perform additional I2C/FIFO work.
     */

    particleSensor.check();


    if (particleSensor.available())
    {
        lastIR =
            particleSensor.getFIFOIR();

        lastRed =
            particleSensor.getFIFORed();

        particleSensor.nextSample();
    }


    // Use the most recent valid values.
    uint32_t ir = lastIR;
    uint32_t red = lastRed;


    // ========================================================
    // MPU6500
    // ========================================================

    xyzFloat acc =
        myMPU6500.getGValues();


    float accMagnitude = sqrt(
        acc.x * acc.x +
        acc.y * acc.y +
        acc.z * acc.z
    );


    // ========================================================
    // GSR
    // ========================================================

    /*
     * IMPORTANT:
     *
     * Only ONE ADC conversion per output sample.
     *
     * No delay().
     * No 10-sample blocking average.
     */

    int gsrValue =
        analogRead(GSR_PIN);


    // ========================================================
    // JSON
    // ========================================================

    doc.clear();


    doc["ir"] =
        ir;


    doc["red"] =
        red;


    doc["accMagnitude"] =
        accMagnitude;


    doc["eda"] =
        gsrValue;


    // ========================================================
    // SERIAL OUTPUT
    // ========================================================

    serializeJson(
        doc,
        Serial
    );

    Serial.println();


    // ========================================================
    // ESP32 RATE MONITOR
    // ========================================================

    /*
     * Prints the actual ESP32 output rate once every
     * 10 seconds.
     *
     * This is NOT printed for every sample, so it won't
     * significantly affect the sampling rate.
     */

    rateCounter++;


    uint32_t elapsed =
        millis() - rateStart;


    if (elapsed >= 10000)
    {
        float actualRate =
            (rateCounter * 1000.0f) /
            elapsed;

        Serial.print(
            "{\"status\":\"rate\",\"hz\":"
        );

        Serial.print(
            actualRate,
            2
        );

        Serial.println(
            "}"
        );


        rateCounter = 0;

        rateStart = millis();
    }
}