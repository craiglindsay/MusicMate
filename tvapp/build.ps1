# Build the MusicMate TV wrapper and drop the APK where the server serves it.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tvapp\build.ps1
#
# Tooling lives outside the repo in D:\Tools (portable JDK 17, Android
# command-line tools with platform 34 / build-tools 34, Gradle 8.9); none of
# it needs administrator rights. The first run creates the signing key.
$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Tools = 'D:\Tools'
$env:JAVA_HOME = (Get-ChildItem "$Tools\jdk" -Directory | Select-Object -First 1).FullName
$env:ANDROID_HOME = "$Tools\android"
$env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
$env:PATH = "$env:JAVA_HOME\bin;$env:ANDROID_HOME\platform-tools;$env:PATH"
$Gradle = (Get-ChildItem "$Tools\gradle" -Directory | Select-Object -First 1).FullName + '\bin\gradle.bat'

New-Item -ItemType Directory -Force -Path "$Here\app\keystore" | Out-Null
$Ks = "$Here\app\keystore\musicmate.jks"
if (-not (Test-Path $Ks)) {
    & "$env:JAVA_HOME\bin\keytool.exe" -genkeypair -v -keystore $Ks -storepass musicmate -keypass musicmate `
        -alias musicmate -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=MusicMate, O=Home, C=US"
}
Set-Location $Here
& $Gradle --no-daemon -q assembleRelease
$apk = "$Here\app\build\outputs\apk\release\app-release.apk"
if (-not (Test-Path $apk)) { throw "no APK produced" }
New-Item -ItemType Directory -Force -Path "$Here\..\jukebox\dist" | Out-Null
Copy-Item $apk "$Here\..\jukebox\dist\mm.apk" -Force          # short: easy to type with a remote
"built " + (Get-Item "$Here\..\jukebox\dist\mm.apk").Length + " bytes -> jukebox\dist\mm.apk"
