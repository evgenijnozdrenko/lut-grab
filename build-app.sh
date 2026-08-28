#!/bin/bash
# Збирає «LUT Grab.app» і DMG для роздачі.
# Потрібен pyinstaller: python3 -m venv .venv && .venv/bin/pip install pyinstaller
set -euo pipefail
cd "$(dirname "$0")"

PYI="${PYI:-.venv/bin/pyinstaller}"
[ -x "$PYI" ] || { echo "Немає pyinstaller. Постав: python3 -m venv .venv && .venv/bin/pip install pyinstaller"; exit 1; }

NAME="LUT Grab"
# Іконку перезбираємо, якщо є з чого: скрипт і джерело живуть у приватному репо
# бренду, а сюди icon.icns кладеться вже готовим.
if [ -f make-icon.py ]; then
  [ -x .venv/bin/python ] && .venv/bin/python make-icon.py || python3 make-icon.py
fi

"$PYI" --noconfirm --clean --windowed --name "$NAME" \
  --add-data "$PWD/ui.html:." \
  --icon "$PWD/icon.icns" \
  --collect-all webview \
  --osx-bundle-identifier click.lootai.grab \
  --distpath dist --workpath build --specpath build \
  server.py

# DMG збираємо з окремої теки: у ній .app і ярлик на /Applications, щоб людина
# перетягнула одне в друге і на цьому все скінчилось.
STAGE="build/dmg"
rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -R "dist/$NAME.app" "$STAGE/"
ln -sfn /Applications "$STAGE/Applications"
# Інструкція лежить поруч у самому образі: без підпису Apple перший запуск
# упирається в Gatekeeper, і без цього файлу програма виглядає зламаною.
cp "ЯК ВІДКРИТИ.txt" "$STAGE/"
rm -f "dist/$NAME.dmg"
hdiutil create -volname "$NAME" -srcfolder "$STAGE" -ov -format UDZO "dist/$NAME.dmg" >/dev/null

echo
echo "  Готово:"
echo "  dist/$NAME.app   $(du -sh "dist/$NAME.app" | cut -f1)"
echo "  dist/$NAME.dmg   $(du -sh "dist/$NAME.dmg" | cut -f1)"
echo
echo '  Бандл підписаний ad-hoc. Без Apple Developer ID ($99/рік) macOS покаже'
echo "  попередження при першому запуску — див. CLAUDE.md, розділ про Gatekeeper."
