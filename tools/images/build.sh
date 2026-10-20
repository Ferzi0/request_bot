#!/bin/sh
# Пересборка картинок бота: HTML-шаблон → скриншот Chrome headless → JPEG (sips, есть в macOS).
# Запуск из любой папки:  sh tools/images/build.sh            — все картинки
#                          sh tools/images/build.sh in_work    — только перечисленные
# Строки Chrome «task_policy_set … invalid argument» на macOS безобидны; важно, что снимок создан.
# Нужны: Google Chrome в /Applications и сайт рядом с ботом (../ferz-site — шрифты и портрет).
set -eu
# Без раскрытия «*» в аргументах: имена проверяются по белому списку ниже.
set -f
HERE=$(cd "$(dirname "$0")" && pwd)
OUT="$HERE/../../bot/assets"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SITE="$HERE/../../../ferz-site/assets"
[ -x "$CHROME" ] || { echo "Нет Google Chrome: $CHROME" >&2; exit 1; }
[ -d "$SITE/fonts" ] || { echo "Нет сайта рядом с ботом: $SITE" >&2; exit 1; }
# Без портрета приветствие молча собралось бы пустым местом справа — лучше остановиться.
[ -f "$SITE/portrait/portrait-waist-960.png" ] || { echo "Нет портрета: $SITE/portrait/portrait-waist-960.png" >&2; exit 1; }
ALL="welcome accepted in_work"
NAMES=${*:-$ALL}
for name in $NAMES; do
  case " $ALL " in *" $name "*) ;; *) echo "Нет такой картинки: $name (есть: $ALL)" >&2; exit 1;; esac
done
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
for name in $NAMES; do
  # Отдельный профиль во временной папке — не трогаем профиль и окна Chrome пользователя.
  # virtual-time-budget даёт шрифтам загрузиться до снимка.
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
    --user-data-dir="$TMP/profile" --allow-file-access-from-files --virtual-time-budget=4000 \
    --window-size=1280,720 --screenshot="$TMP/$name.png" "file://$HERE/$name.html" >/dev/null
  [ -s "$TMP/$name.png" ] || { echo "Chrome не создал снимок $name.png" >&2; exit 1; }
  sips -s format jpeg -s formatOptions 82 "$TMP/$name.png" --out "$OUT/$name.jpg" >/dev/null
  echo "$OUT/$name.jpg: $(wc -c < "$OUT/$name.jpg") байт"
done
