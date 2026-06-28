APP="dist/GrAF Explorer.app"
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
"$LSREGISTER" -f "$APP"          # re-register the app + its declared types
touch "$APP"                      # nudge the bundle's mod-time
killall Finder                    # let Finder redraw
