# co-Link Standard for Windows x64

1. Download the Standard Setup ZIP from https://colink.namanavi.com/dongle.
2. Extract the whole ZIP and run **Install co-Link.cmd**. Do not run it from
   inside the ZIP viewer. Installation is for the current Windows user.
3. If you need wireless DualSense, select the optional USB/IP driver in Setup.
   The lightweight web installer downloads the pinned signed upstream installer,
   checks its SHA-256 and Authenticode signature, then shows its installer.
   This step needs internet and Windows administrator approval. Audio-only users
   do not need USB/IP. No virtual audio cable is needed.
4. Open **co-Link Standard** from the Start menu. If your dongle has not been
   prepared as a transmitter, use the app's conversion button or the website.
   Compatibility inspection and original-driver backup come before payment.
5. Select your transmitter and pair your audio device or DualSense in the app.
   Codec availability follows the connected device and installed plugins.

The Standard app is free. Optional website conversion costs US$7.99 once per
dongle; the same successfully registered dongle can be reconverted without
another payment. Conversion changes that dongle's Windows driver binding, so
devices using it can disconnect. Use a separate dongle if you need the normal
Windows Bluetooth stack at the same time. Keep the original-driver backup.

System audio uses WASAPI loopback of the chosen Windows output; selecting it
does not create a virtual output or automatically mute the original speakers.
Select the appropriate Windows playback output and start audio before capture.

This release bundles public SBC/SBC XQ, AAC, aptX-family and LDAC adapters.
Adaptive adapters are shells only: external encoder/helper files are NOT bundled.
See the packaged external-codec guide for exact reference combinations and test
limits. No LE Audio or universal hardware/codec support is promised.

The app itself is not Authenticode-signed yet. Windows may show an unknown
publisher/reputation warning. Download only from the official site and compare
SHA256SUMS.txt. Do not disable Windows security settings. Optional USB/IP uses
its signed upstream installer. Standard, Developer and Steam use distinct
installation/settings identities; Standard contains no offline converter.

Corresponding LGPL library sources and rebuild instructions are available free:
https://colink.namanavi.com/downloads/co-Link-LGPL-Sources-0.1.2.zip

Support: ghdtjrtka@gmail.com
