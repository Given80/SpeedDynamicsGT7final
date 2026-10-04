[README_DELTA.txt](https://github.com/user-attachments/files/33028132/README_DELTA.txt)
SPEED DYNAMICS GT7 – LIVE DELTA POSITION VERSION

Dateien:
- SpeedDynamicsGT7.py
- web/index.html

Wichtig:
1. Diese Version ersetzt die bisherige SpeedDynamicsGT7.py.
2. web/index.html ebenfalls ersetzen.
3. Keine separate *_LIVE_DELTA*.py Datei zusätzlich starten.
4. Der Delta-Vergleich wird NICHT mehr als aktuelle Rundenzeit minus Bestzeit berechnet.
5. Die App zeichnet eine vollständige gefahrene Runde mit GT7-Weltposition X/Z und Zeit auf.
6. Die schnellste vollständig aufgezeichnete Runde wird als Referenz verwendet.
7. In der nächsten Runde wird die aktuelle Position mit derselben Position der Referenzrunde verglichen.
8. Deshalb bleibt das Delta während der Runde ein echtes Live-Delta und läuft nicht einfach von einem großen Minuswert auf 0.
9. Für den ersten korrekten Vergleich muss mindestens eine vollständige Runde mit der App aufgezeichnet werden.
10. Das bestehende Dashboard-Design und die RPM-Einstellungen wurden beibehalten.
