# AnchorHold Web Viewer
<img width="1232" height="692" alt="anchorhold web viewer interface" src="https://github.com/user-attachments/assets/d4a9b325-164c-43c7-95dc-c786bfccff76" /><img width="1920" height="1080" alt="shoalmark2" src="https://github.com/user-attachments/assets/d5a1fc5c-e9bf-418f-9895-727d9e4c3840" />
Charts for boaters, in a browser. Bathymetry, side scan sonar and substrate
over satellite imagery, tide-corrected depth, and an anchor watch that
alarms if the boat drags (requires gps on device). Everything runs local, offline once charts are fetched

Built for shallow water surveying with commercial fish finders, recordings made by
**Shoalmark ASV** carrying a Humminbird Helix 7 MEGA SI.
[PINGMapper](https://github.com/CameronBodine/PINGMapper) decodes the
recording and produces the georeferenced mosaic.

**[Full user guide at droneboatfleet.com](https://www.droneboatfleet.com/anchorhold-web-viewer/)**,
covering every stage from planning survey transects to each chart overlay.

## Chart Overlays

- **Depth.** Bathymetry shaded by depth, with contours drawn on top at an
  interval set in Settings. Contours follow the depth toggle.
- **Sonar.** The side scan mosaic. Brightness, contrast and pixel smoothing
  adjust while the chart is in view, and are saved per survey.
- **Substrate.** The PINGMapper bottom classification, with its own key. Work


  in progress.
- **Rock.** [RockMapper](https://github.com/PINGEcosystem/RockMapper) habitat
  classes, fines through bedrock, drawn above substrate. Work in progress.
- **Objects.** [GhostVision](https://github.com/PINGEcosystem/GhostVision)
  detections as hollow rings, so the evidence underneath stays visible. Work
  in progress.
- Shallow-water shading below a chosen depth, survey outlines, tracklines and
  a pin per surveyed area, none of which need a toggle.

## Planning a Survey

**Survey Planner** ships in this repository, under `SurveyPlanner/`. It fetches
a waterbody outline from the
[USGS National Hydrography Dataset](https://www.usgs.gov/national-hydrography/national-hydrography-dataset),
takes the places a boat can actually be put in, and turns the water into
straight survey transects split into outings that fit a day or one battery
charge.

- Regions of interest and no-go areas around docks and moorings, with a
  separate setback for each, cut out before any line is planned.
- Transits routed around peninsulas rather than assumed straight, with the
  clearance check measuring the whole mission track.
- A live estimate of days, miles of line and computation time that follows
  every parameter, so the cost of closer spacing is visible before the run.
- Exports a [QGroundControl](https://qgroundcontrol.com/) `.plan` and a
  [GPX](https://www.topografix.com/gpx.asp) track, one file per day, plus
  GeoJSON of the whole plan for GIS.

Built on [Tkinter](https://docs.python.org/3/library/tkinter.html) and
[matplotlib](https://matplotlib.org/) alone. It has its own dependencies, which
`SurveyPlanner\run.bat` installs on first run. Opening a `.shp` outline
additionally needs `pip install pyshp`.

## Building a Survey

- **Seven recording formats.** Humminbird `.DAT` natively, and Lowrance
  `.sl2`/`.sl3`, Garmin `.RSD`, EdgeTech `.jsf`, Cerulean `.svlog` and `.xtf`
  through [PINGVerter](https://github.com/CameronBodine/PINGVerter).
- **Recording Fixer.** Rebuilds the `.DAT` header a power cut leaves missing,
  flags position jumps, frozen fixes, depth spikes and turns, and takes marks
  drawn on the map. Writes a time filter rather than a new recording, so side
  scan survives, and the files off the card are never modified.
- **One build chain.** Decode, tile, contour, grid and legend, producing
  MBTiles, GeoJSON, tap-to-query grids and a chart record per survey. A time
  filter left beside a recording is found and applied without being attached.
- **Chart bundles.** Create a sharable survey bundle with overlays
- **A workflow list** in the app, five steps with a launcher each, and counts
  read from disk rather than remembered.

## Running It

Requires [Python](https://www.python.org/downloads/) 3.10 or newer on `PATH`,
and [Chrome](https://www.google.com/chrome/) or
[Edge](https://www.microsoft.com/edge).  Opera works too. Building charts additionally needs
PINGMapper in its own conda environment, conventionally named `ping`.

```
pip install -r pipeline\requirements.txt
run_web_app.bat
```

The app opens at `http://localhost:8000/`. `python pipeline\web_server.py`
does the same without the launcher.

| Launcher | Does |
| --- | --- |
| `run_web_app.bat` | Serves the app and opens it |
| `SurveyPlanner\run.bat` | Survey Planner. `selftest.bat` beside it checks the planner |
| `Fix_Recording.bat` | The Recording Fixer |
| `Add_Survey_Locations.bat` | The build screen |
| `Add_Web_Charts.bat` | Chart library from a shell: `list`, `add`, `remove`, `default` |
| `Make_Chart_Bundle.bat` | `build <survey folder>`, `install-web <bundle>` |
| `build_web_package.bat` | A portable copy of the app, `--zip` for an archive |
| `build_web_release.bat` | Release zips, `-all` to include the charts |

`http://localhost:8000/?selftest=1` exercises the app and writes the result
into the page title. All ten steps should report `ok`.

**Recordings and builds live outside this repository**, in machine-level
folders remembered in `%LOCALAPPDATA%\AnchorHold\workspace.json`. Builds
default to `~/AnchorHold/output` and are set with
`workspace.set_output_dir(path)` or `ANCHORHOLD_OUTPUT`. Recordings are set
from step 2 of the workflow list. A checkout that already holds an `output/`
directory with surveys in it keeps using that one.

Each optional program is reached by path, and an environment variable
overrides the guess: `PINGMAPPER_PYTHON`, `ROCKMAPPER_PYTHON`,
`GHOSTVISION_PYTHON`.


## Licence

[MIT](LICENSE). Copyright (c) 2026 Maximilian K Schwartz IV.

The only third-party code inside this repository is
[MapLibre GL JS](https://github.com/maplibre/maplibre-gl-js) 4.7.1,
BSD-3-Clause, with its notice at `web/vendor/LICENSE-maplibre.txt`.

Two optional programs are installed by hand and are not distributed here.
**GhostVision is GPL-3.0.** **RockMapper states no licence**, which by default
means all rights reserved. Skipping either removes only its overlay.
`pipeline/ghost_vision.py`, the glue that drives GhostVision, is MIT like the
rest of this repository; the combination exists only on a machine where both
are installed, and anyone redistributing the two together should read GPL-3.0
first.
