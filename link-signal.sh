#!/bin/zsh
# Link this Mac as a SECONDARY device on your existing Signal account.
# No new phone number needed.
#
# This opens a QR code as an image (Preview). On your phone:
#   Signal → Settings → Linked Devices → "+"  → scan it.
# The link URI expires after ~1 min, so have your phone ready BEFORE running.

# JVM signal-cli build (the Homebrew native build has a send-breaking bug).
export JAVA_HOME=/opt/homebrew/opt/openjdk
SIGNAL_CLI="$(dirname "$0")/signal-cli-dist/bin/signal-cli"
QR_PNG="$(dirname "$0")/link-qr.png"

echo "Have phone Signal open at: Settings → Linked Devices → '+' (camera ready)."
echo "Generating link QR…"
echo

# signal-cli prints the link URI, then blocks waiting for your phone to scan.
# We capture the URI, render a big PNG, open it, and let signal-cli keep waiting.
"$SIGNAL_CLI" link -n "mac-mini" | while IFS= read -r line; do
    case "$line" in
        sgnl://*|tsdevice:*)
            qrencode -s 10 -m 3 -o "$QR_PNG" "$line"
            open "$QR_PNG"
            echo "QR opened in Preview — scan it now. Waiting for your phone…"
            ;;
        *)
            echo "$line"
            ;;
    esac
done
