#!/bin/bash
# Подвійний клік по цьому файлу запускає yt-grab і відкриває вкладку в браузері.
cd "$(dirname "$0")" || exit 1
exec /usr/bin/env python3 server.py
