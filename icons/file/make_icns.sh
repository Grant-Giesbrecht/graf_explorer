mkdir document.iconset
sips -z 16 16     graf_file.png --out document.iconset/icon_16x16.png
sips -z 32 32     graf_file.png --out document.iconset/icon_16x16@2x.png
sips -z 32 32     graf_file.png --out document.iconset/icon_32x32.png
sips -z 64 64     graf_file.png --out document.iconset/icon_32x32@2x.png
sips -z 128 128   graf_file.png --out document.iconset/icon_128x128.png
sips -z 256 256   graf_file.png --out document.iconset/icon_128x128@2x.png
sips -z 256 256   graf_file.png --out document.iconset/icon_256x256.png
sips -z 512 512   graf_file.png --out document.iconset/icon_256x256@2x.png
sips -z 512 512   graf_file.png --out document.iconset/icon_512x512.png
sips -z 1024 1024 graf_file.png --out document.iconset/icon_512x512@2x.png
iconutil -c icns document.iconset
rm -rf document.iconset
