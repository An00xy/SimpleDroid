SimpleDroid - Minimal Windows 11 Android App Player
===================================================

How to Use SimpleDroid.exe
--------------------------
1. Double-click SimpleDroid.exe.
2. On first launch, SimpleDroid checks Windows Hypervisor Platform (WHPX).
   - If disabled, click "Open Windows Features", check "Windows Hypervisor Platform",
     click OK, restart Windows, and click "Retry Setup".
3. SimpleDroid automatically reuses an existing Android SDK / JDK 17+ if installed,
   or downloads official Google Android SDK tools and portable JDK 17.
4. Click "Start Android" to boot Android 15.
5. Drag and drop any .apk file (or click "+ Install APK") to install & launch apps.
6. Use the "+ Add to Phone" card or "Phone Files" tab to send files directly to
   /sdcard/Download inside the Android device.
7. Optional: Click "Associate .apk" in the bottom-left sidebar so double-clicking
   any .apk file in Windows Explorer opens and installs it in SimpleDroid.

Low-End PC Tips
---------------
- Open the "Settings" tab and click "Optimize for Low-End PC".
- Choose "Phone 720p" or "Phone 540p", 2 GB RAM, and "Android 11 (Ultra-Light)"
  for the fastest performance on 4-8 GB RAM / dual-core laptops.

Troubleshooting
---------------
- Logs are saved at: %LOCALAPPDATA%\SimpleDroid\logs\emulator.log
- You can also inspect logs directly from the "Logs" tab inside the app.
- Use Settings -> "Reset Android (Wipe Data)" if you ever want a fresh device.
