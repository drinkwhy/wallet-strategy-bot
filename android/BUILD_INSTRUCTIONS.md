# SolTrader Android App — Build Instructions

## Prerequisites (install these first)
1. Android Studio: https://developer.android.com/studio
2. JDK 17+: comes with Android Studio
3. Node.js (for bubblewrap): https://nodejs.org

---

## Step 1 — Deploy your web app first
Your Flask app MUST be live on HTTPS before building the Android app.

Fastest free option (Railway):
1. Go to https://railway.app → New Project → Deploy from GitHub
2. Upload your BOT folder
3. Set env vars (copy from your .env file)
4. Railway gives you a URL like: https://soltrader-production.up.railway.app

Or buy your domain at Namecheap: soltrader.app (~$14/yr)

---

## Step 2 — Generate signing keystore
Open a terminal and run:
```
keytool -genkey -v -keystore soltrader-keystore.jks -alias soltrader -keyalg RSA -keysize 2048 -validity 10000
```
Save the .jks file inside the `android/` folder.

---

## Step 3 — Get your keystore SHA-256 fingerprint
```
keytool -list -v -keystore soltrader-keystore.jks -alias soltrader
```
Copy the SHA-256 fingerprint (looks like: AB:CD:12:34:...)

---

## Step 4 — Update assetlinks.json
In app.py, find the `assetlinks` route and replace:
  "REPLACE_WITH_YOUR_KEYSTORE_SHA256"
with your actual SHA-256 fingerprint (no colons, lowercase):
  "ab:cd:12:34:ef:56:..."

Deploy the updated app so https://YOUR_DOMAIN/.well-known/assetlinks.json returns your fingerprint.

---

## Step 5 — Update twa-manifest.json
Edit `android/twa-manifest.json`:
- Change "host" to your actual domain (e.g. "soltrader.app")
- Change iconUrl/maskableIconUrl to your actual icon URLs

---

## Step 6 — Build with bubblewrap (easiest method)
```
npm install -g @bubblewrap/cli
cd android
bubblewrap init --manifest https://YOUR_DOMAIN/manifest.json
bubblewrap build
```
This generates: `android/app-release-signed.aab`

---

## Step 7 — OR build manually with Gradle
```
cd android
./gradlew bundleRelease
```
Output: `android/app/build/outputs/bundle/release/app-release.aab`

---

## Step 8 — Submit to Google Play
1. Go to https://play.google.com/console
2. Create developer account ($25 one-time fee)
3. Create new app → Upload the .aab file
4. Fill out store listing, screenshots, privacy policy
5. Submit for review (1-3 days)

---

## App icons needed
Create these PNG files and place in android/app/src/main/res/:
- mipmap-hdpi/ic_launcher.png     (72x72)
- mipmap-mdpi/ic_launcher.png     (48x48)
- mipmap-xhdpi/ic_launcher.png    (96x96)
- mipmap-xxhdpi/ic_launcher.png   (144x144)
- mipmap-xxxhdpi/ic_launcher.png  (192x192)
- drawable/splash.png             (512x512, dark navy background with S logo)

Use https://icon.kitchen to generate all sizes from one image.
