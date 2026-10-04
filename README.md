[README_DELTA_LIVE_V2.txt](https://github.com/user-attachments/files/33025132/README_DELTA_LIVE_V2.txt)
Speed Dynamics GT7 – Delta Live V2

Replace these two files in the existing repository:
1. SpeedDynamicsGT7.py (repository root)
2. web/index.html (inside the existing web folder)

Do NOT change the existing GitHub Actions workflow.
The workflow already builds the Windows EXE with --add-data "web;web".

Delta behavior:
- Records complete laps with GT7 position coordinates.
- Keeps the fastest complete recorded lap as spatial reference.
- During the current lap, compares the car against the reference at the same track position.
- Updates the browser dashboard about 4 times per second.
- Negative delta = faster; positive delta = slower.
- The first complete lap after starting this version becomes the initial reference.
