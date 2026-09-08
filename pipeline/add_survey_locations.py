"""
add_survey_locations.py

Adds surveyed locations to AnchorHold.

Pick a depth CSV, a substrate raster and the sonar mosaic tiles for a place,
run the pipeline on them, and everything lands in one folder per survey:

    <builds>/<id>/    bathymetry, sonar, substrate and rock MBTiles,
                      contours, shallow bands, boundary and track,
                      the depth and substrate query grids, the legends
    <builds>/locations.json   the register this window keeps

<builds> is workspace.output_dir(), which is neutral ground rather than a
folder inside this checkout, so the same surveys are reachable from any
program that reads them.

Nothing here installs a chart into an app. AnchorHold Web Viewer finds a
built survey by looking in that folder: Settings, then Charts on this
computer, then Add. Make chart bundle packs one into a single file for a
machine that did not build it.

Run:  python add_survey_locations.py   (or Add_Survey_Locations.bat from the repo root)
"""

import glob
from collections import namedtuple
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

import chart_bundle              # packs a built survey into one file
import merge_locations
import workspace                 # the recordings folder, set once
import appicon                   # the window icon, on every window

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

pipeline_dir = os.path.dirname(os.path.abspath(__file__))
repo_dir = os.path.dirname(pipeline_dir)
PROCESS_SCRIPT = os.path.join(pipeline_dir, 'process_data.py')
OUTPUT_ROOT = workspace.output_dir()
# A register of the surveys built on this machine: names, pins, settings and
# which layers each one has. The browser app does not read it. Charts reach
# the app through Settings, which looks in output/ for anything not already
# in the library. The register exists so this window remembers its work.
CATALOG_PATH = os.path.join(OUTPUT_ROOT, 'locations.json')

CATALOG_VERSION = 1
DEFAULT_ZOOM = 16.0
DEFAULT_TIMEZONE = 'America/Hermosillo'
DEFAULT_WATER_TEMP = 10.0

DEPTH_SCRIPT = os.path.join(pipeline_dir, 'depth_csv_from_sonar.py')
ROCK_SCRIPT = os.path.join(pipeline_dir, 'rock_map.py')
GHOST_SCRIPT = os.path.join(pipeline_dir, 'ghost_vision.py')
# PINGVerter's SUPPORTED_SONAR_EXTENSIONS, and nothing else: a filter that
# offers a format the converter cannot read is a decode failure minutes in.
RECORDING_EXTS = ('.dat', '.sl2', '.sl3', '.rsd', '.svlog', '.jsf', '.xtf')
DEPTH_INPUT_FILETYPES = [
    ("Recording or CSV", "*.DAT *.sl2 *.sl3 *.RSD *.svlog *.jsf *.xtf *.csv"),
    ("Sonar recordings", "*.DAT *.sl2 *.sl3 *.RSD *.svlog *.jsf *.xtf"),
    ("CSV files", "*.csv"),
    ("All files", "*.*"),
]

# PINGMapper needs GDAL and lives in its own conda env (the one PINGWizard.bat
# activates), so the extractor runs as a subprocess with that interpreter.
PINGMAPPER_PYTHON_CANDIDATES = [
    os.environ.get('PINGMAPPER_PYTHON', ''),
    os.path.join(os.path.expanduser('~'), 'miniforge3', 'envs', 'ping', 'python.exe'),
    os.path.join(os.path.expanduser('~'), 'anaconda3', 'envs', 'ping', 'python.exe'),
    os.path.join(os.path.expanduser('~'), 'miniconda3', 'envs', 'ping', 'python.exe'),
]

# RockMapper ships in its own conda env (python -m pinginstaller rockmapper).
ROCKMAPPER_PYTHON_CANDIDATES = [
    os.environ.get('ROCKMAPPER_PYTHON', ''),
    os.path.join(os.path.expanduser('~'), 'miniforge3', 'envs', 'rockmapper', 'python.exe'),
    os.path.join(os.path.expanduser('~'), 'anaconda3', 'envs', 'rockmapper', 'python.exe'),
    os.path.join(os.path.expanduser('~'), 'miniconda3', 'envs', 'rockmapper', 'python.exe'),
]

# GhostVision ships in its own conda env too (its repo has the yml).
GHOSTVISION_PYTHON_CANDIDATES = [
    os.environ.get('GHOSTVISION_PYTHON', ''),
    os.path.join(os.path.expanduser('~'), 'miniforge3', 'envs', 'ghostvision', 'python.exe'),
    os.path.join(os.path.expanduser('~'), 'anaconda3', 'envs', 'ghostvision', 'python.exe'),
    os.path.join(os.path.expanduser('~'), 'miniconda3', 'envs', 'ghostvision', 'python.exe'),
]

# Files the pipeline produces, and where each one belongs in the app.
GRID_FILES = ['depth_grid.bin', 'depth_grid.json',
              'substrate_grid.bin', 'substrate_grid.json',
              'contours.geojson', 'shallow_bands.geojson',
              'detections.geojson']
TILE_FILES = ['bathymetry.mbtiles', 'sonar.mbtiles', 'substrate.mbtiles', 'rock.mbtiles']
# Kilobyte-sized, and what an un-downloaded survey shows on the map, so these
# ride in the app itself rather than in the survey's on-demand pack.
PREVIEW_FILES = ['boundary.geojson', 'track.geojson']

# Every knob PINGMapper and RockMapper leave open, with what it does and what
# to set it to. The window shows these; a survey stores whatever was set.
#
# A preset is nothing more than a set of these values with a name on it, so
# picking one and then changing a field is an ordinary thing to do rather than
# a special case: the survey records the values, and the preset name is a label
# for how they got there.
Param = namedtuple('Param', 'key flag kind default label explain advice choices')
Param.__new__.__defaults__ = ((),)

PARAMS = {
    # ── how the side scan mosaic is toned ──────────────────────────────────
    'mosaic': (
        Param('egn', '--egn', 'bool', True,
              'Even out the gain (EGN)',
              'Divides every ping by an average taken across the whole survey at '
              'the same range. That is what removes the bright ribbon under the '
              'boat and the dark outer edges of each pass - and therefore what '
              'makes two passes match where they overlap instead of showing a '
              'seam.',
              'On. It is the single setting that most decides whether a mosaic '
              'is readable.'),
        Param('clahe', '--clahe', 'bool', True,
              'Local contrast (CLAHE)',
              'Stretches contrast within small tiles rather than across the whole '
              'image, so detail survives in the bright parts and the dark parts '
              'at once.',
              'On, alongside EGN. Measured against a hand-tuned run: +72% '
              'neighbouring-pixel detail, +0.85 bits of entropy.'),
        Param('db_transform', '--db-transform', 'bool', False,
              'Decibel transform',
              'Takes 20*log10 of the amplitudes before the 8-bit mapping, lifting '
              'quiet returns out of the dark end.',
              'Off. On its own it helps, but stacked with EGN and CLAHE the three '
              'normalisations compound until the sea floor is black: measured '
              'detail collapses from 19.5 to 2.2.'),
        Param('clahe_clip', '--clahe-clip', 'float', 0.02,
              'CLAHE clip limit',
              'How hard the local stretch may push before it is clipped. Higher '
              'is punchier and noisier.',
              '0.02. Past about 0.05 the speckle starts to look like texture.'),
        Param('egn_stretch', '--egn-stretch', 'choice', 'percent',
              'Stretch after EGN',
              'What to do with the range of values EGN leaves behind: nothing, '
              'stretch between the extremes, or stretch between percentiles.',
              'percent - the extremes in sonar are almost always outliers.',
              ('none', 'minmax', 'percent')),
        Param('egn_stretch_factor', '--egn-stretch-factor', 'float', 0.5,
              'Percent clipped from each tail',
              'How much of each end of the histogram the percent stretch throws '
              'away, as a percentage.',
              '0.5.'),
        Param('pix_res_son', '--pix-res-son', 'float', 0.0,
              'Mosaic pixel size (m)',
              "0 keeps the recording's own resolution and skips the resample. A "
              'number here fixes how much ground one pixel covers.',
              '0. Resampling costs detail, and nothing downstream needs a fixed '
              'scale.'),
        Param('tone_gamma', '--tone-gamma', 'float', 1.0,
              'Gamma',
              'Below 1 brightens the mid-tones, above 1 darkens them. 1 is off.',
              '1. Reach for this only when a mosaic is right but flat.'),
        Param('tone_gain', '--tone-gain', 'float', 1.0,
              'Gain',
              'Overall brightness multiplier applied after EGN. 1 is off.',
              '1.'),
    ),

    # ── which pings reach the imagery ──────────────────────────────────────
    'track': (
        Param('speed_correct', '--speed-correct', 'bool', False,
              'Correct for boat speed',
              'Resamples along-track so ground covered slowly is not stretched '
              'out in the image. It changes the imagery only; the soundings are '
              'written from every ping either way.',
              'On for anything but a very steady run.'),
        Param('min_speed', '--min-speed', 'float', 0.0,
              'Drop pings slower than (m/s)',
              'A boat barely moving paints the same ground again and again. 0.3 '
              'm/s is 0.7 mph; 0.5 m/s is 1.1 mph. 0 keeps everything.',
              '0.3 m/s (0.7 mph) for a survey run at 2 mph.'),
        Param('max_heading_deviation', '--max-heading-deviation', 'float', 0.0,
              'Drop turns sharper than (deg)',
              'Judged over the distance in the next field, so both have to be set '
              'for the filter to do anything. The ends of every lawnmower leg are '
              'where it bites.',
              '20 degrees, with 10 m below. 0 turns it off.'),
        Param('max_heading_distance', '--max-heading-distance', 'float', 0.0,
              'measured over (m)',
              'The distance the heading change above is measured across. 10 m is '
              '33 ft.',
              '10 m (33 ft).'),
    ),

    # ── how substrate is classified ────────────────────────────────────────
    'substrate': (
        Param('substrate_class', '--substrate-class', 'choice', 'max',
              'Classifier',
              "'max' gives every pixel its most likely class. 'thresh' instead "
              'promotes gravel wherever its probability clears 0.14 and '
              'cobble/boulder wherever it clears 0.35, which finds more hard '
              'bottom and gets some of it wrong.',
              "max, unless you are hunting hard bottom and will check the result.",
              ('max', 'thresh')),
        Param('substrate_res', '--substrate-res', 'float', 0.0,
              'Map pixel size (m)',
              "0 keeps the recording's own resolution. PINGMapper's own default "
              'is 0.25 m.',
              '0. Set 0.25 only if something downstream wants a fixed scale.'),
        Param('substrate_polygons', '--substrate-polygons', 'bool', False,
              'Also export polygons',
              'Writes the classified map as shapefiles beside the raster, for GIS '
              'work.',
              'Off. The app draws the raster; the shapefiles are extra minutes '
              'and extra files.'),
    ),

    # ── how finely rock is predicted ───────────────────────────────────────
    'rock': (
        Param('window_m', '--window-m', 'float', 18.0,
              'Prediction window (m)',
              'The square RockMapper classifies in one go. 9 m is 30 ft, 18 m is '
              '59 ft, 30 m is 98 ft. A smaller window sees smaller rock patches '
              'and takes proportionally longer.',
              '18 m (59 ft). Drop to 9 m when you are after individual boulders.'),
        Param('window_stride', '--window-stride', 'int', 6,
              'Window stride',
              'How many steps the window takes across its own width. Higher means '
              'more overlap, smoother edges and more compute.',
              '6.'),
        Param('min_area_percent', '--min-area-percent', 'float', 0.75,
              'Smallest patch kept (% of window)',
              'Classified areas smaller than this share of a window are dropped '
              'as noise.',
              '0.75.'),
        Param('min_patch_size', '--min-patch-size', 'int', 5,
              'Smallest patch kept (pixels)',
              'The same idea in pixels, applied to the raster.',
              '5, or 3 with a fine window.'),
        Param('smooth_tol_m', '--smooth-tol-m', 'float', 0.3,
              'Polygon smoothing (m)',
              'How far an outline may move when it is simplified. 0.3 m is about '
              'a foot.',
              '0.3 m, or 0.15 with a fine window.'),
        Param('batch_size', '--batch-size', 'int', 30,
              'Prediction batch size',
              'How many windows go to the model at once. Larger is faster and '
              'wants more memory.',
              '30. Drop it if RockMapper runs out of memory.'),
    ),
    # ── what GhostVision counts as a detection ─────────────────────────────
    'ghost': (
        Param('confidence', '--confidence', 'float', 0.5,
              'Sure enough to call it (0-1)',
              'How certain the model has to be before an object is marked. '
              'Lower finds more and marks more rubbish; higher marks only what '
              'looks unmistakable.',
              '0.5. Drop to 0.3 for a first look at what is down there, raise '
              'to 0.7 when you only want the certain ones.'),
        Param('track', '--no-track', 'bool', True,
              'Track objects across windows',
              'The same patch of bottom is looked at by many overlapping '
              'windows. Tracking ties those looks together so one object is one '
              'detection rather than thirty.',
              'On. Without it every window reports separately and the map fills '
              'with duplicates.'),
        Param('track_count', '--track-count', 'int', 17,
              'Seen in this many windows',
              'How many consecutive windows must agree before a track is '
              'believed. This is the main filter on false positives.',
              '17, which is what GhostVision ships with. Fewer finds more and '
              'trusts less.'),
        Param('alpha', '--alpha', 'float', 0.45,
              'Best look against average',
              "Weighting between a track's best single look and its average "
              'when scoring it. Higher leans on the best look.',
              '0.45.'),
        Param('iou_threshold', '--iou-threshold', 'float', 0.1,
              'Boxes this close are one object',
              'How much two detection boxes may overlap before they are treated '
              'as the same thing.',
              '0.1.'),
        Param('window_stride', '--window-stride', 'float', 0.05,
              'Window step (fraction)',
              'How far the moving window advances each time, as a fraction of '
              'its own length. Smaller means more overlap, more looks per '
              'object, and proportionally more compute.',
              '0.05. Raise it to 0.2 for a quick pass.'),
        Param('images', '--images', 'bool', False,
              'Write the detection images',
              'Saves the sonogram tiles with boxes drawn on them, which is how '
              'you check whether a detection is a crab pot, a rock or a stump.',
              'On the first run over new water, off afterwards - it is a lot of '
              'files.'),
        Param('gpx_to_card', '--gpx-to-card', 'bool', False,
              'Waypoints back to the SD card',
              'Writes the detections as GPX beside the recording, in the shape '
              "the Humminbird reads, so they show up as waypoints on the boat's "
              'own plotter.',
              'On when you intend to go back and look at them.'),
    ),
}

# A preset is a named set of values - only the ones it changes are listed, the
# rest are the parameter defaults above.
PRESETS = {
    'mosaic': [
        ('best', 'Best image (EGN + CLAHE)',
         {'egn': True, 'clahe': True, 'db_transform': False}),
        ('egn', 'EGN only (even gain, no local contrast)',
         {'egn': True, 'clahe': False, 'db_transform': False}),
        ('raw', 'PINGMapper defaults (no corrections)',
         {'egn': False, 'clahe': False, 'db_transform': False}),
    ],
    'track': [
        ('none', 'Off - keep every ping', {}),
        ('speed', 'Correct for boat speed', {'speed_correct': True}),
        ('slow', 'Speed + drop under 0.7 mph',
         {'speed_correct': True, 'min_speed': 0.3}),
        ('turns', 'Speed, drop under 1.1 mph and turns over 20 deg',
         {'speed_correct': True, 'min_speed': 0.5,
          'max_heading_deviation': 20.0, 'max_heading_distance': 10.0}),
    ],
    'substrate': [
        ('standard', 'Standard (most likely class)', {}),
        ('hard', 'Favour hard bottom (thresholded)', {'substrate_class': 'thresh'}),
        ('quarter', 'Standard, 0.25 m pixels', {'substrate_res': 0.25}),
        ('polygons', 'Standard + polygons', {'substrate_polygons': True}),
    ],
    'rock': [
        ('standard', 'Standard (59 ft window)', {}),
        ('fine', 'Fine detail (30 ft window)',
         {'window_m': 9.0, 'min_patch_size': 3, 'smooth_tol_m': 0.15}),
        ('coarse', 'Coarse (98 ft window)',
         {'window_m': 30.0, 'min_patch_size': 8, 'smooth_tol_m': 0.6}),
    ],
    # The model was trained on derelict crab pots in Delaware coastal water.
    # Anywhere else it is a detector of things that look like one, so the
    # settings here are really about how much you want to be shown.
    'ghost': [
        ('careful', 'Careful (only the certain ones)', {}),
        ('wide', 'Wide net (more marks, more rubbish)',
         {'confidence': 0.3, 'track_count': 8, 'images': True}),
        ('quick', 'Quick pass (bigger steps)',
         {'window_stride': 0.2, 'track_count': 5}),
    ],
}

GROUP_TITLES = {'mosaic': 'Mosaic image', 'track': 'Keep pings',
                'substrate': 'Substrate', 'rock': 'Rock detail',
                'ghost': 'Bottom objects'}

CUSTOM = 'Custom (values below)'


def default_values(group):
    return {p.key: p.default for p in PARAMS[group]}


def preset_values(group, key):
    """The full set of values a preset stands for."""
    values = default_values(group)
    for name, _label, overrides in PRESETS[group]:
        if name == key:
            values.update(overrides)
            break
    return values


def preset_label(group, key):
    for name, label, _values in PRESETS[group]:
        if name == key:
            return label
    return CUSTOM


def preset_key(group, label):
    for name, text, _values in PRESETS[group]:
        if text == label:
            return name
    return 'custom'


def matching_preset(group, values):
    """Which preset these values are, if they are one."""
    for name, _label, _overrides in PRESETS[group]:
        if preset_values(group, name) == values:
            return name
    return 'custom'


def default_processing():
    """Every group at its default preset, as a survey would store it."""
    return {group: {'preset': PRESETS[group][0][0],
                    'values': preset_values(group, PRESETS[group][0][0])}
            for group in PARAMS}


def choice_of(processing, group):
    """One group's stored choice, filled out and repaired."""
    saved = (processing or {}).get(group)
    if isinstance(saved, str):                 # the shape before the fields existed
        return {'preset': saved, 'values': preset_values(group, saved)}
    values = default_values(group)
    if isinstance(saved, dict):
        values.update({k: v for k, v in (saved.get('values') or {}).items()
                       if k in values})
        if not saved.get('values') and saved.get('preset'):
            values = preset_values(group, saved['preset'])
    return {'preset': matching_preset(group, values), 'values': values}


def describe_choice(group, choice):
    """What the button says: the preset's name, or that it is not one."""
    return (preset_label(group, choice['preset'])
            if choice['preset'] != 'custom' else CUSTOM)


def flags_for(group, values):
    """
    The command-line flags one group's values come to.

    A flag that begins --no- turns something off, so the switch is passed
    when the setting is false rather than true: the window says 'Track
    objects' and the tool takes --no-track, and neither has to be phrased
    backwards for the other's sake.
    """
    flags = []
    for param in PARAMS[group]:
        value = values.get(param.key, param.default)
        if param.kind == 'bool':
            wanted = not value if param.flag.startswith('--no-') else bool(value)
            if wanted:
                flags.append(param.flag)
        else:
            flags += [param.flag, _text(param, value)]
    return flags


def _text(param, value):
    if param.kind == 'int':
        return str(int(float(value)))
    if param.kind == 'float':
        return repr(float(value))
    return str(value)


def processing_of(entry):
    """
    A survey's processing choices, with anything it does not name filled in.

    A survey built before one of these choices existed gets the default for it,
    which is the setting it should have been built with anyway.
    """
    saved = dict(entry.get('processing') or {})
    # The first shape this took stored only the mosaic tone, with the speed
    # correction as a checkbox beside it.
    older = entry.get('mosaic') or {}
    if older.get('preset') and not saved.get('mosaic'):
        saved['mosaic'] = older['preset']
    if older.get('speedCorrect') and not saved.get('track'):
        saved['track'] = 'speed'
    return {group: choice_of(saved, group) for group in PARAMS}


def time_filter_for(recording: str) -> str:
    """
    The Fixer's time filter for this recording, if it wrote one.

    A repaired recording is named <stem>_fixed and its filter
    <stem>_fixed_timefilter.csv, so the file sits beside whichever of the
    two you point at. Found rather than asked for: having already made
    the edit, nobody should have to attach it as well.
    """
    if not recording or not os.path.isfile(recording):
        return ''
    stem = os.path.splitext(recording)[0]
    for candidate in (stem + '_timefilter.csv',
                      stem + '_fixed_timefilter.csv'):
        if os.path.isfile(candidate):
            return candidate
    return ''


def decode_flags(processing, sonar_mosaic=False, substrate_map=False,
                 time_filter=''):
    """The depth_csv_from_sonar.py flags for one survey's choices."""
    flags = []
    if time_filter:
        flags += ['--time-filter', time_filter]
    if sonar_mosaic:
        flags += flags_for('mosaic', processing['mosaic']['values'])
    flags += flags_for('track', processing['track']['values'])
    if substrate_map:
        flags += flags_for('substrate', processing['substrate']['values'])
    return flags


def rock_flags(processing):
    """The rock_map.py flags for one survey's choices."""
    return flags_for('rock', processing['rock'])


def ghost_flags(processing):
    """The ghost_vision.py flags for one survey's choices."""
    return flags_for('ghost', processing['ghost']['values'])


def decode_settings(processing, sonar_mosaic=False, substrate_map=False):
    """
    What those flags resolve to, asked of the tool that will run them.

    Resolving them there rather than restating them here means the two cannot
    drift: a preset that changes changes in one place, and what is compared
    against an earlier run is what that run actually recorded.
    """
    import depth_csv_from_sonar
    args = depth_csv_from_sonar.parse_args(
        ['(settings only)'] + decode_flags(processing, sonar_mosaic, substrate_map))
    return {'image': depth_csv_from_sonar.image_settings(args),
            'substrate': depth_csv_from_sonar.substrate_settings(args)}


def cached_decode_settings(csv_path):
    """What an earlier decode recorded about how it was run."""
    suffix = "_depth.csv"
    if not csv_path or not csv_path.endswith(suffix):
        return {}
    project = os.path.basename(csv_path)[: -len(suffix)]
    manifest = os.path.join(os.path.dirname(csv_path), project + "_products.json")
    try:
        with open(manifest) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return {'image': data.get('imageSettings'),
            'substrate': data.get('substrateSettings')}


def same_processing(cached, wanted, sonar_mosaic=False, substrate_map=False):
    """
    Whether an earlier decode was made the way this build is asking for.

    Only the parts being asked for count: a substrate raster classified some
    other way does not matter to a build that is not making one.
    """
    if sonar_mosaic and cached.get('image') != wanted['image']:
        return False
    if substrate_map and cached.get('substrate') != wanted['substrate']:
        return False
    return True

# The original survey, which keeps its flat asset names from before this GUI existed.
LEGACY_ENTRY = {
    'id': 'santa-rosalia',
    'name': 'Santa Rosalía',
    'center': {'lat': 27.338, 'lon': -112.263},
    'zoom': DEFAULT_ZOOM,
    'timezone': DEFAULT_TIMEZONE,
    'tide': {'mode': 'guaymas'},
    'inputs': {'csv': '', 'substrate': '', 'sonar': []},
    'tiles': {'bathymetry': 'bathymetry.mbtiles', 'sonar': 'sonar.mbtiles',
              'substrate': 'substrate.mbtiles', 'rock': 'rock.mbtiles'},
    'grids': {'depth': 'depth_grid', 'substrate': 'substrate_grid'},
    'contours': 'contours.geojson',
    'shallowBands': 'shallow_bands.geojson',
}


def slugify(name):
    slug = re.sub(r'[^a-z0-9]+', '-', name.strip().lower()).strip('-')
    return slug or 'location'


def is_recording(path):
    return bool(path) and os.path.splitext(path)[1].lower() in RECORDING_EXTS


def find_pingmapper_python():
    """An interpreter with PINGMapper installed."""
    for path in PINGMAPPER_PYTHON_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    try:
        import pingmapper  # noqa: F401  (only to see whether this python has it)
        return sys.executable
    except ImportError:
        pass
    raise RuntimeError(
        "No python with PINGMapper found. Set PINGMAPPER_PYTHON to the "
        "interpreter of the conda environment PINGMapper is installed in "
        "(PINGWizard.bat shows which one that is).")


def check_recording_data(recording):
    """
    A Humminbird .DAT is only a 64-byte header; the pings live in a folder of the
    same name beside it. Say so up front instead of letting PINGMapper fail with
    "Out of SON files" several screens into its output.
    """
    if os.path.splitext(recording)[1].lower() != '.dat':
        return
    son_dir = os.path.splitext(recording)[0]
    if os.path.isdir(son_dir) and glob.glob(os.path.join(son_dir, '*.SON')):
        return
    raise RuntimeError(
        f"No sonar data next to that recording.\n\n"
        f"{os.path.basename(recording)} is just the header; the pings live in\n"
        f"  {son_dir}\\B00*.SON\n"
        "which is missing or empty. Point at the .DAT that still sits beside its "
        "B001.SON folder - PINGMapper moves recordings into its project folder, "
        "so the copy under pingmapper/output/<project>/ is often the live one.")


def conda_env_environment(python_exe):
    """
    The environment variables `conda activate` would set for that interpreter.

    Running an env's python.exe directly is not enough on Windows: GDAL's DLLs
    live in <env>\\Library\\bin and its data files in <env>\\Library\\share, so
    without these the process dies at import with a bare exit code 127.
    """
    env_root = os.path.dirname(os.path.abspath(python_exe))
    env = dict(os.environ)

    prefixes = [
        env_root,
        os.path.join(env_root, 'Library', 'mingw-w64', 'bin'),
        os.path.join(env_root, 'Library', 'usr', 'bin'),
        os.path.join(env_root, 'Library', 'bin'),
        os.path.join(env_root, 'Scripts'),
        os.path.join(env_root, 'bin'),
    ]
    env['PATH'] = os.pathsep.join(
        [p for p in prefixes if os.path.isdir(p)] + [env.get('PATH', '')])

    gdal_data = os.path.join(env_root, 'Library', 'share', 'gdal')
    proj_data = os.path.join(env_root, 'Library', 'share', 'proj')
    if os.path.isdir(gdal_data):
        env['GDAL_DATA'] = gdal_data
    if os.path.isdir(proj_data):
        env['PROJ_LIB'] = proj_data
        env['PROJ_DATA'] = proj_data
    env['CONDA_PREFIX'] = env_root
    return env


def run_pingmapper_products(recording, out_dir, log, cancel,
                            temp=DEFAULT_WATER_TEMP, auto_depth=False,
                            sonar_mosaic=False, substrate_map=False,
                            processing=None):
    """
    Produce the survey layers locally from a raw recording.

    Depth is always decoded (seconds). The side scan mosaic and the substrate map
    are opt-in because they are the slow parts of a PINGMapper run - minutes, and
    gigabytes of intermediate tiles.

    Returns {'csv': path, 'sonar': [paths], 'substrate': [paths]}.
    """
    check_recording_data(recording)
    python = find_pingmapper_python()
    project = os.path.splitext(os.path.basename(recording))[0]
    work_dir = os.path.join(out_dir, 'pingmapper')
    csv_path = os.path.join(work_dir, f"{project}_depth.csv")
    os.makedirs(work_dir, exist_ok=True)

    # -u: unbuffered, so the log streams a line at a time instead of in
    # 8 KB gulps that make a long step look like a hang.
    cmd = [python, '-u', DEPTH_SCRIPT, recording,
           '--out-dir', work_dir, '--project', project, '--temp', str(float(temp))]
    if auto_depth:
        cmd += ['--depth-source', 'auto']
    if sonar_mosaic:
        cmd += ['--sonar-mosaic']
    if substrate_map:
        cmd += ['--substrate-map']
    # Tone, track filtering and substrate classification. Left off, PINGMapper
    # writes a mosaic with the nadir ribbon and edge falloff still in it, and
    # no two passes match where they overlap.
    processing = processing or default_processing()
    # An edit made in the Recording Fixer travels as a file beside the
    # recording; PINGMapper applies it, so all this has to do is notice.
    time_filter = time_filter_for(recording)
    cmd += decode_flags(processing, sonar_mosaic, substrate_map, time_filter)

    wanted = ['depth map'] + (['side scan mosaic'] if sonar_mosaic else [])         + (['substrate map'] if substrate_map else [])
    log("Running PINGMapper for: " + ", ".join(wanted) + " ...")
    if sonar_mosaic:
        log("  mosaic image: " + describe_choice('mosaic', processing['mosaic']))
    log("  track filter: " + describe_choice('track', processing['track']))
    if time_filter:
        log("  time filter:  " + os.path.basename(time_filter)
            + " - only the stretches it names are read")
    if substrate_map:
        log("  substrate:    " + describe_choice('substrate',
                                                 processing['substrate']))
    log("  " + " ".join(cmd) + "\n")
    proc = subprocess.Popen(cmd, cwd=repo_dir, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            errors='replace', env=conda_env_environment(python))
    try:
        for line in proc.stdout:
            log(line.rstrip())
            if cancel.is_set():
                proc.terminate()
                raise RuntimeError("Stopped by user")
    finally:
        proc.stdout.close()
    if proc.wait() != 0:
        raise RuntimeError("PINGMapper could not decode that recording - see the log above")
    if not os.path.isfile(csv_path):
        raise RuntimeError(f"No depth CSV produced at {csv_path}")
    products = {'csv': csv_path, 'sonar': [], 'substrate': []}
    manifest = os.path.join(work_dir, f"{project}_products.json")
    if os.path.isfile(manifest):
        try:
            with open(manifest) as f:
                data = json.load(f)
            products['csv'] = data.get('csv') or csv_path
            products['sonar'] = list(data.get('sonar') or [])
            products['substrate'] = list(data.get('substrate') or [])
        except (OSError, ValueError):
            pass

    log(f"\nDepth CSV: {products['csv']}")
    if sonar_mosaic:
        log(f"Side scan mosaic: {len(products['sonar'])} tile(s)")
    if substrate_map:
        log(f"Substrate map: {len(products['substrate'])} raster(s)")
    log("")
    return products


def cached_products(csv_path):
    """
    What an earlier PINGMapper run left next to [csv_path], read from the
    manifest depth_csv_from_sonar.py writes beside the CSV. Files that have
    since been deleted are dropped, so a stale manifest cannot slip a
    missing mosaic into a build.

    Returns (sonar_paths, substrate_paths); both empty when there is nothing.
    """
    suffix = "_depth.csv"
    if not csv_path or not csv_path.endswith(suffix):
        return [], []
    project = os.path.basename(csv_path)[: -len(suffix)]
    manifest = os.path.join(os.path.dirname(csv_path), project + "_products.json")
    if not os.path.isfile(manifest):
        return [], []
    try:
        with open(manifest) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return [], []
    present = lambda paths: [p for p in (paths or []) if os.path.isfile(p)]
    return present(data.get("sonar")), present(data.get("substrate"))


def find_rockmapper_python():
    """An interpreter with RockMapper installed (its own conda env)."""
    for path in ROCKMAPPER_PYTHON_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    raise RuntimeError(
        "No python with RockMapper found.\n\n"
        "Install it with:\n"
        "  pip install --force-reinstall pinginstaller\n"
        "  python -m pinginstaller rockmapper\n\n"
        "or set ROCKMAPPER_PYTHON to the interpreter of the env holding it.")


def find_ghostvision_python():
    """An interpreter with GhostVision installed (its own conda env)."""
    for path in GHOSTVISION_PYTHON_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    raise RuntimeError(
        "No python with GhostVision found." + chr(10) + chr(10) +
        "Install it from https://github.com/PINGEcosystem/GhostVision:" + chr(10) +
        "  conda env create -f ghostvision/conda/ghostvision_install.yml" + chr(10) +
        "  conda activate ghostvision" + chr(10) +
        "  pip install ." + chr(10) + chr(10) +
        "or set GHOSTVISION_PYTHON to the interpreter of the env holding it.")


def run_ghost_vision(recording, out_dir, project, log, cancel, processing=None):
    """
    Look for objects on the bottom with GhostVision, and keep the points.

    This reads the recording again from scratch: the detector wants side scan
    with the water column left in and 16-bit tiles, which is not what the chart
    build makes, so it builds a project of its own. That is minutes, on top of
    everything else, which is why it is off unless asked for.

    Returns the path of the detections GeoJSON, or '' when nothing was found.
    """
    python = find_ghostvision_python()
    work_dir = os.path.join(out_dir, 'ghostvision')
    os.makedirs(work_dir, exist_ok=True)
    processing = processing or default_processing()

    cmd = [python, '-u', GHOST_SCRIPT,
           '--recording', recording,
           '--out-dir', work_dir,
           '--project', project]
    cmd += ghost_flags(processing)

    log("Running GhostVision over the recording ...")
    log("  detection: " + describe_choice('ghost', processing['ghost']))
    log("  " + " ".join(cmd) + chr(10))
    proc = subprocess.Popen(cmd, cwd=repo_dir, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            errors='replace', env=conda_env_environment(python))
    try:
        for line in proc.stdout:
            log(line.rstrip())
            if cancel.is_set():
                proc.terminate()
                raise RuntimeError("Stopped by user")
    finally:
        proc.stdout.close()
    if proc.wait() != 0:
        raise RuntimeError("GhostVision failed - see the log above")

    manifest = os.path.join(work_dir, f'{project}_ghost.json')
    found = ''
    count = 0
    if os.path.isfile(manifest):
        try:
            with open(manifest) as f:
                data = json.load(f)
            found = data.get('geojson', '')
            count = int(data.get('count', 0))
        except (OSError, ValueError):
            found = ''
    if not found or not os.path.isfile(found):
        log(chr(10) + "GhostVision found nothing to mark." + chr(10))
        return ''

    # The chart build reads one name for this, next to the other overlays.
    beside = os.path.join(out_dir, 'detections.geojson')
    shutil.copy2(found, beside)
    log(f'{chr(10)}{count} detection(s): {beside}{chr(10)}')
    return beside


def run_rock_map(sonar_files, out_dir, project, log, cancel, epsg=0,
                 processing=None):
    """
    Predict rocky habitat from a survey's side scan mosaics with RockMapper.

    [sonar_files] are the mosaics this survey actually uses - the ones PINGMapper
    just made, or the ones picked by hand - so a folder holding other surveys'
    tiles cannot leak into the prediction. Runs rock_map.py inside the
    `rockmapper` conda env, which has its own model stack. Returns the raster.
    """
    python = find_rockmapper_python()
    os.makedirs(out_dir, exist_ok=True)
    cmd = [python, '-u', ROCK_SCRIPT,
           '--out-dir', out_dir, '--project', project,
           '--sonar'] + list(sonar_files)
    if epsg:
        cmd += ['--epsg', str(int(epsg))]
    processing = processing or default_processing()
    cmd += rock_flags(processing)

    log("Running RockMapper over the sonar mosaics ...")
    log("  rock detail: " + describe_choice('rock', processing['rock']))
    log("  " + " ".join(cmd) + "\n")
    proc = subprocess.Popen(cmd, cwd=repo_dir, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            errors='replace', env=conda_env_environment(python))
    try:
        for line in proc.stdout:
            log(line.rstrip())
            if cancel.is_set():
                proc.terminate()
                raise RuntimeError("Stopped by user")
    finally:
        proc.stdout.close()
    if proc.wait() != 0:
        raise RuntimeError("RockMapper failed - see the log above")

    manifest = os.path.join(out_dir, f"{project}_rock.json")
    raster = ''
    if os.path.isfile(manifest):
        try:
            with open(manifest) as f:
                raster = json.load(f).get('raster', '')
        except (OSError, ValueError):
            raster = ''
    if not raster:
        raise RuntimeError("RockMapper produced no habitat raster")
    log(f"\nRock habitat raster: {raster}\n")
    return raster


# Columns every depth source carries, whether it is a PINGMapper meta CSV or the
# slim one depth_csv_from_sonar.py writes.
DEPTH_COLUMNS = ['lon', 'lat', 'dep_m', 'date', 'time']


def resolve_input(path):
    """Absolute path for a catalog input, which may be relative to the repo."""
    if not path:
        return ''
    if os.path.isabs(path):
        return path if os.path.isfile(path) else ''
    candidate = os.path.join(repo_dir, path)
    return candidate if os.path.isfile(candidate) else ''


def combine_depth_csv(csv_paths, out_csv, log=print):
    """
    Concatenate several surveys' soundings into one CSV the pipeline can read.

    Only the five columns the pipeline uses are kept, so a raw meta CSV and a
    slim depth CSV combine cleanly despite their different layouts.
    """
    import pandas as pd          # heavy: imported here so the GUI opens without it

    frames = []
    for path in csv_paths:
        try:
            frame = pd.read_csv(path, usecols=DEPTH_COLUMNS).dropna()
        except ValueError as exc:
            raise RuntimeError(f"{os.path.basename(path)} has no usable soundings "
                               f"({exc})") from exc
        log(f"  {os.path.basename(path)}: {len(frame):,} soundings")
        frames.append(frame)
    if not frames:
        raise RuntimeError("Nothing to combine")

    merged = pd.concat(frames, ignore_index=True)
    os.makedirs(os.path.dirname(out_csv), exist_ok=True)
    merged.to_csv(out_csv, index=False)
    log(f"  -> {len(merged):,} soundings in {out_csv}")
    return out_csv


def merge_rasters(paths, out_tif, log=print):
    """
    Merge classified rasters - substrate or rock - into one covering both surveys.

    They share a class scheme, so the first raster's colormap carries over; without
    it the tiler would paint the merged map in greyscale class indices.
    """
    import rasterio
    from rasterio.merge import merge as rasterio_merge

    paths = [p for p in paths if p and os.path.isfile(p)]
    if not paths:
        return ''
    if len(paths) == 1:
        return paths[0]

    sources = [rasterio.open(p) for p in paths]
    try:
        mosaic, transform = rasterio_merge(sources)
        profile = sources[0].profile
        try:
            colormap = sources[0].colormap(1)
        except ValueError:
            colormap = None
    finally:
        for src in sources:
            src.close()

    profile.update(height=mosaic.shape[1], width=mosaic.shape[2],
                   count=1, transform=transform, compress='lzw')
    os.makedirs(os.path.dirname(out_tif), exist_ok=True)
    with rasterio.open(out_tif, 'w', **profile) as dst:
        dst.write(mosaic[0], 1)
        if colormap:
            dst.write_colormap(1, colormap)
    log(f"  merged {len(paths)} rasters -> {out_tif}")
    return out_tif


def combine_entries(entries, name, out_root, log=print):
    """
    Build one survey out of several: soundings concatenated, mosaics pooled,
    substrate and rock rasters merged.

    The result is an ordinary catalog entry pointing at the merged inputs, so
    Build runs the normal pipeline over it and produces a single chart - one
    depth grid, contours that cross the seam - rather than stitching outputs.
    """
    if len(entries) < 2:
        raise RuntimeError("Pick at least two surveys to combine")

    entry_id = slugify(name)
    inputs_dir = os.path.join(out_root, entry_id, 'inputs')

    csvs = [resolve_input(e['inputs'].get('csv', '')) for e in entries]
    missing = [e['name'] for e, c in zip(entries, csvs) if not c]
    if missing:
        raise RuntimeError("No depth CSV on record for: " + ", ".join(missing)
                           + ". Build those surveys first, then combine them.")

    log("Combining soundings:")
    merged_csv = combine_depth_csv(csvs, os.path.join(inputs_dir, 'combined_depth.csv'), log)

    sonar = []
    for e in entries:
        for path in e['inputs'].get('sonar') or []:
            found = resolve_input(path)
            if found and found not in sonar:
                sonar.append(found)

    substrate = merge_rasters([resolve_input(e['inputs'].get('substrate', '')) for e in entries],
                              os.path.join(inputs_dir, 'combined_substrate.tif'), log)
    rock = merge_rasters([resolve_input(e['inputs'].get('rock', '')) for e in entries],
                         os.path.join(inputs_dir, 'combined_rock.tif'), log)

    first = entries[0]
    timezones = {e.get('timezone') for e in entries}
    tides = {json.dumps(e.get('tide') or {'mode': 'none'}, sort_keys=True) for e in entries}
    if len(timezones) > 1:
        log(f"  NOTE: timezones differ ({', '.join(sorted(timezones))}); "
            f"keeping {first.get('timezone')}")
    if len(tides) > 1:
        log("  NOTE: the surveys disagree on tide; keeping the first one's setting")

    combined = blank_entry(name)
    combined.update({
        'id': entry_id,
        'timezone': first.get('timezone', DEFAULT_TIMEZONE),
        'waterTempC': first.get('waterTempC', DEFAULT_WATER_TEMP),
        'tide': dict(first.get('tide') or {'mode': 'none'}),
        'zoom': first.get('zoom', DEFAULT_ZOOM),
        # Everything is already made; Build just tiles it.
        'generate': {'sonar': False, 'substrate': False, 'rock': False},
        'inputs': {'recording': '', 'csv': merged_csv,
                   'substrate': substrate, 'sonar': sonar},
        'combinedFrom': [e['id'] for e in entries],
    })
    if rock:
        combined['inputs']['rock'] = rock
    log("")
    log(f"Combined survey '{name}': {len(sonar)} mosaic(s), "
        f"substrate {'yes' if substrate else 'no'}, "
        f"rock {'yes' if rock else 'no'}")
    return combined


def blank_entry(name='New location'):
    return {
        'id': slugify(name),
        'name': name,
        'center': None,
        'zoom': DEFAULT_ZOOM,
        'timezone': DEFAULT_TIMEZONE,
        'waterTempC': DEFAULT_WATER_TEMP,
        'tide': {'mode': 'none'},   # safe default: no tide until proven otherwise
        'generate': {'sonar': False, 'substrate': False, 'rock': False,
                     'ghost': False},
        'processing': default_processing(),
        'inputs': {'recording': '', 'csv': '', 'substrate': '', 'sonar': []},
        'tiles': {},
        'grids': {},
        'contours': None,
        'shallowBands': None,
        'detections': None,
    }


# ── deriving tide + timezone from coordinates ─────────────────────────────────

def timezone_for(lat, lon, log=print):
    """
    IANA timezone for a coordinate.

    Uses timezonefinder when it is installed, otherwise asks the keyless
    timeapi.io service, and falls back to a fixed UTC offset from the longitude
    (good enough to reduce soundings, but it has no DST).
    """
    try:
        from timezonefinder import TimezoneFinder
        tz = TimezoneFinder().timezone_at(lat=lat, lng=lon)
        if tz:
            log(f"  timezonefinder -> {tz}")
            return tz
    except ImportError:
        pass

    try:
        import requests
        r = requests.get("https://timeapi.io/api/TimeZone/coordinate",
                         params={"latitude": lat, "longitude": lon}, timeout=8)
        if r.ok:
            tz = r.json().get("timeZone")
            if tz:
                log(f"  timeapi.io -> {tz}")
                return tz
    except Exception as exc:
        log(f"  timeapi.io lookup failed ({exc})")

    offset = int(round(lon / 15.0))
    # Etc/GMT signs are inverted: 105°W (UTC-7) is "Etc/GMT+7".
    tz = f"Etc/GMT{'+' if offset <= 0 else '-'}{abs(offset)}"
    log(f"  no lookup available - using fixed offset {tz} (no daylight saving)")
    return tz


def tide_for(lat, lon, api_key='', log=print):
    """
    Tide setting for a coordinate, as the catalog stores it.

    With a WorldTides API key we fit real harmonic constants for this exact
    spot (via extract_constants.py). Without one we cannot invent them, so the
    location is marked as having no tide - correct for lakes and reservoirs,
    and honest for the sea until constants are supplied.
    """
    if not api_key:
        log("  no WorldTides API key - marking this location as 'no tide'.\n"
            "  For coastal water, add a key (worldtides.info) and detect again,\n"
            "  or paste constants from pipeline/extract_constants.py.")
        return {'mode': 'none'}

    cmd = [sys.executable, os.path.join(pipeline_dir, 'extract_constants.py'),
           '--lat', str(lat), '--lon', str(lon), '--api-key', api_key, '--json']
    log("  fitting harmonic constants: " + " ".join(cmd[:-3] + ['--api-key', '***', '--json']))
    proc = subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True)
    if proc.returncode != 0:
        log(proc.stdout[-2000:] + proc.stderr[-2000:])
        raise RuntimeError("extract_constants.py failed - see the log above")
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    log(f"  fitted {len(payload)} constituents")
    return {'mode': 'constituents',
            'constituents': [[c['speed'], c['amp'], c['phase']] for c in payload]}


def center_from_grid(grid_json_path):
    """Middle of the depth grid described by a pipeline *_grid.json header."""
    with open(grid_json_path) as f:
        g = json.load(f)
    lon = g['lonMin'] + g['dLon'] * g['cols'] / 2.0
    lat = g['latMin'] + g['dLat'] * g['rows'] / 2.0
    return {'lon': round(lon, 6), 'lat': round(lat, 6)}


def center_from_csv(csv_path):
    """Middle of the surveyed track, straight from the soundings CSV."""
    import csv as _csv
    lons, lats = [], []
    with open(csv_path, newline='') as f:
        reader = _csv.DictReader(f)
        for row in reader:
            try:
                lons.append(float(row['lon']))
                lats.append(float(row['lat']))
            except (TypeError, ValueError, KeyError):
                continue
    if not lons:
        return None
    return {'lon': round((min(lons) + max(lons)) / 2, 6),
            'lat': round((min(lats) + max(lats)) / 2, 6)}


# ── worker dialog ─────────────────────────────────────────────────────────────

class TaskDialog(tk.Toplevel):
    """Runs `work(log)` on a thread and streams its output into a log window."""

    def __init__(self, parent, title, work, on_done):
        super().__init__(parent)
        self.title(title)
        self.geometry("760x460")
        self.transient(parent)
        self.on_done = on_done
        self.log_queue = queue.Queue()
        self.cancel_flag = threading.Event()

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill='both', expand=True)
        self.progress = ttk.Progressbar(frm, mode='indeterminate')
        self.progress.pack(fill='x')
        self.progress.start(12)
        # A step can be legitimately quiet for minutes (merging a 200 MB mosaic
        # is one call with nothing to say). Showing how long it has run, and how
        # long since the last line, is what tells a slow run from a stuck one.
        self.var_status = tk.StringVar(value="Starting ...")
        ttk.Label(frm, textvariable=self.var_status, foreground='#555555').pack(
            anchor='w', pady=(4, 0))
        self.started_at = time.time()
        self.last_output_at = self.started_at
        self.finished = False
        self._tick()
        # The log needs a scrollbar: a build prints hundreds of lines and the
        # interesting one - a skipped mosaic, a warning - is usually well above
        # the bottom by the time you look.
        log_frame = ttk.Frame(frm)
        log_frame.pack(fill='both', expand=True, pady=(8, 8))
        self.txt = tk.Text(log_frame, height=20, wrap='word', state='disabled',
                           background='#111111', foreground='#dddddd')
        scroll = ttk.Scrollbar(log_frame, orient='vertical', command=self.txt.yview)
        self.txt.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.txt.pack(side='left', fill='both', expand=True)
        # Wheel over the log scrolls the log, whatever has focus.
        self.txt.bind('<MouseWheel>',
                      lambda e: (self.txt.yview_scroll(-e.delta // 120, 'units'), 'break')[1])
        row = ttk.Frame(frm)
        row.pack(fill='x')
        self.btn_stop = ttk.Button(row, text="Stop", command=self.cancel_flag.set)
        self.btn_stop.pack(side='left')
        self.btn_close = ttk.Button(row, text="Close", command=self.destroy, state='disabled')
        self.btn_close.pack(side='right')

        threading.Thread(target=self._work, args=(work,), daemon=True).start()
        self.after(100, self._drain)

    def _work(self, work):
        try:
            self.log_queue.put(('__done__', work(self.log_queue.put, self.cancel_flag)))
        except Exception as exc:
            self.log_queue.put(('__error__', str(exc)))

    def _drain(self):
        try:
            while True:
                item = self.log_queue.get_nowait()
                if isinstance(item, tuple):
                    self._finish(*item)
                    return
                self._append(item)
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _finish(self, kind, payload):
        self.finished = True
        self.var_status.set(f"Finished in {self._clock(time.time() - self.started_at)}")
        self.progress.stop()
        self.btn_stop.configure(state='disabled')
        self.btn_close.configure(state='normal')
        if kind == '__done__':
            self._append("\nFinished.")
            self.on_done(payload)
        else:
            self._append(f"\nFailed: {payload}")
            messagebox.showerror("Locations", str(payload), parent=self)

    def _tick(self):
        """Once a second, refresh the elapsed/idle readout."""
        if self.finished:
            return
        now = time.time()
        quiet = now - self.last_output_at
        note = f" - quiet for {quiet:.0f}s" if quiet > 10 else ""
        self.var_status.set(f"Running {self._clock(now - self.started_at)}{note}")
        self.after(1000, self._tick)

    @staticmethod
    def _clock(seconds):
        return f"{int(seconds) // 60}m {int(seconds) % 60:02d}s"

    def _append(self, text):
        self.last_output_at = time.time()
        line = str(text)
        # Stamp each line with how far into the run it arrived, so a long gap
        # is obvious afterwards as well as while watching.
        stamp = self._clock(self.last_output_at - self.started_at)
        line = f"{stamp:>8}  {line}" if line.strip() else line
        # Follow the tail only while the view is already at the bottom, so
        # scrolling back to read something is not yanked away by the next line.
        at_bottom = self.txt.yview()[1] > 0.999
        self.txt.configure(state='normal')
        self.txt.insert('end', line + chr(10))
        if at_bottom:
            self.txt.see('end')
        self.txt.configure(state='disabled')


# ── main window ───────────────────────────────────────────────────────────────

class ProcessingDialog(tk.Toplevel):
    """
    Every setting for one stage, with what it does and what to set it to.

    A dropdown of presets sits at the top and fills the fields; the fields
    are the truth. Change one and the preset simply reads Custom, because a
    preset here is a set of values with a name on it and nothing more.
    """

    def __init__(self, parent, group, choice, on_ok):
        super().__init__(parent)
        self.group = group
        self.on_ok = on_ok
        self.transient(parent)
        self.title(GROUP_TITLES[group])
        self.geometry('700x620')
        self.resizable(True, True)
        self.filling = False           # while a preset is writing the fields

        top = ttk.Frame(self, padding=(10, 10, 10, 4))
        top.pack(fill='x')
        ttk.Label(top, text='Preset').pack(side='left')
        self.var_preset = tk.StringVar(
            value=describe_choice(group, choice))
        self.cmb = ttk.Combobox(
            top, textvariable=self.var_preset, state='readonly', width=44,
            values=[label for _key, label, _values in PRESETS[group]] + [CUSTOM])
        self.cmb.pack(side='left', padx=(6, 0))
        self.cmb.bind('<<ComboboxSelected>>', self._preset_picked)
        ttk.Label(top, text='fills the fields below', foreground='#777777').pack(
            side='left', padx=(8, 0))

        # A scrolling body: the mosaic alone has nine settings, each with a
        # paragraph, and a window tall enough for all of it would not fit on a
        # laptop screen.
        body = ttk.Frame(self)
        body.pack(fill='both', expand=True, padx=(10, 0), pady=(4, 0))
        canvas = tk.Canvas(body, borderwidth=0, highlightthickness=0)
        bar = ttk.Scrollbar(body, orient='vertical', command=canvas.yview)
        inner = ttk.Frame(canvas, padding=(0, 0, 10, 10))
        inner.bind('<Configure>',
                   lambda _e: canvas.configure(scrollregion=canvas.bbox('all')))
        window = canvas.create_window((0, 0), window=inner, anchor='nw')
        canvas.bind('<Configure>',
                    lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side='left', fill='both', expand=True)
        bar.pack(side='right', fill='y')
        # Bound while the pointer is over the list and released when it
        # leaves: a bind_all that outlived this window would scroll a canvas
        # that no longer exists.
        def wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), 'units')

        canvas.bind('<Enter>', lambda _e: canvas.bind_all('<MouseWheel>', wheel))
        canvas.bind('<Leave>', lambda _e: canvas.unbind_all('<MouseWheel>'))
        self.bind('<Destroy>', lambda _e: canvas.unbind_all('<MouseWheel>'))

        self.vars = {}
        for param in PARAMS[group]:
            self._add_param(inner, param, choice['values'].get(param.key,
                                                               param.default))

        feet = ttk.Frame(self, padding=10)
        feet.pack(fill='x')
        ttk.Button(feet, text='Cancel', command=self.destroy).pack(side='right')
        ttk.Button(feet, text='OK', command=self._ok).pack(side='right', padx=(0, 6))
        ttk.Button(feet, text='Back to recommended',
                   command=self._recommended).pack(side='left')

        self.protocol('WM_DELETE_WINDOW', self.destroy)
        self.bind('<Escape>', lambda _e: self.destroy())
        self.grab_set()

    def _add_param(self, parent, param, value):
        frame = ttk.Frame(parent)
        frame.pack(fill='x', pady=(8, 0))
        if param.kind == 'bool':
            var = tk.BooleanVar(value=bool(value))
            ttk.Checkbutton(frame, text=param.label, variable=var,
                            command=self._edited).pack(anchor='w')
        else:
            row = ttk.Frame(frame)
            row.pack(fill='x')
            ttk.Label(row, text=param.label, width=32).pack(side='left')
            var = tk.StringVar(value=str(value))
            if param.kind == 'choice':
                ttk.Combobox(row, textvariable=var, state='readonly', width=12,
                             values=list(param.choices)).pack(side='left')
            else:
                ttk.Entry(row, textvariable=var, width=12).pack(side='left')
            var.trace_add('write', lambda *_a: self._edited())
        ttk.Label(frame, text=param.explain, wraplength=620, justify='left',
                  foreground='#555555').pack(anchor='w', padx=(18, 0))
        ttk.Label(frame, text='Recommended: ' + param.advice, wraplength=620,
                  justify='left', foreground='#2f6f3f').pack(anchor='w',
                                                             padx=(18, 0))
        self.vars[param.key] = var

    def _preset_picked(self, _event=None):
        key = preset_key(self.group, self.var_preset.get())
        if key == 'custom':
            return
        self._fill(preset_values(self.group, key))

    def _recommended(self):
        first = PRESETS[self.group][0][0]
        self.var_preset.set(preset_label(self.group, first))
        self._fill(preset_values(self.group, first))

    def _fill(self, values):
        self.filling = True
        try:
            for param in PARAMS[self.group]:
                value = values.get(param.key, param.default)
                if param.kind == 'bool':
                    self.vars[param.key].set(bool(value))
                else:
                    self.vars[param.key].set(str(value))
        finally:
            self.filling = False

    def _edited(self):
        if self.filling:
            return
        # What is in the fields is what will run, so the preset name follows
        # them rather than the other way round.
        self.var_preset.set(describe_choice(
            self.group, {'preset': matching_preset(self.group, self._values(True)),
                         'values': {}}))

    def _values(self, lenient=False):
        values = {}
        for param in PARAMS[self.group]:
            raw = self.vars[param.key].get()
            if param.kind == 'bool':
                values[param.key] = bool(raw)
            elif param.kind == 'choice':
                values[param.key] = str(raw)
            else:
                try:
                    values[param.key] = (int(float(raw)) if param.kind == 'int'
                                         else float(raw))
                except (TypeError, ValueError):
                    if not lenient:
                        raise ValueError(f"{param.label}: '{raw}' is not a number")
                    values[param.key] = param.default
        return values

    def _ok(self):
        try:
            values = self._values()
        except ValueError as exc:
            messagebox.showerror(GROUP_TITLES[self.group], str(exc), parent=self)
            return
        self.on_ok({'preset': matching_preset(self.group, values),
                    'values': values})
        self.destroy()


class SurveyPickerDialog(tk.Toplevel):
    """Pick two or more surveys and name the survey they turn into."""

    def __init__(self, parent, entries, on_ok, title="Combine surveys",
                 blurb="", action="Combine", suffix="combined"):
        super().__init__(parent)
        self.action = action
        self.suffix = suffix
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.entries = entries
        self.on_ok = on_ok
        self.vars = []

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text=title, font=('', 10, 'bold')).pack(anchor='w')
        ttk.Label(frame, foreground='#666666', wraplength=380, justify='left',
                  text=blurb).pack(anchor='w', pady=(2, 8))

        for entry in entries:
            var = tk.BooleanVar(value=False)
            self.vars.append(var)
            sonar = len(entry['inputs'].get('sonar') or [])
            label = f"{entry['name']}   ({sonar} mosaic tile(s))" if sonar else entry['name']
            ttk.Checkbutton(frame, text=label, variable=var).pack(anchor='w')

        ttk.Label(frame, text=f"Name for the {suffix} survey").pack(anchor='w', pady=(10, 2))
        self.var_name = tk.StringVar(value="")
        ttk.Entry(frame, textvariable=self.var_name, width=42).pack(fill='x')

        buttons = ttk.Frame(frame)
        buttons.pack(fill='x', pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side='right')
        ttk.Button(buttons, text=action, command=self._ok).pack(side='right', padx=(0, 6))

        # Two ticks are the minimum, so suggest a name from whatever is ticked.
        for var in self.vars:
            var.trace_add('write', lambda *_: self._suggest_name())
        self.grab_set()

    def _selected(self):
        return [e for e, v in zip(self.entries, self.vars) if v.get()]

    def _suggest_name(self):
        chosen = self._selected()
        if len(chosen) >= 2 and not self.var_name.get().strip():
            self.var_name.set(f"{chosen[0]['name']} {self.suffix}")

    def _ok(self):
        chosen = self._selected()
        if len(chosen) < 2:
            messagebox.showerror(self.action, "Pick at least two surveys.", parent=self)
            return
        name = self.var_name.get().strip() or f"{chosen[0]['name']} {self.suffix}"
        if any(e['id'] == slugify(name) for e in self.entries):
            messagebox.showerror(self.action,
                                 f"'{name}' already exists - pick another name.",
                                 parent=self)
            return
        self.destroy()
        self.on_ok(chosen, name)


class LocationGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AnchorHold - Add Survey Locations")
        appicon.apply(self)
        self.geometry("1180x820")
        self.minsize(940, 640)

        self.entries = []
        self.current = None
        self.default_id = None
        self.dirty = False

        self._build_vars()
        self._build_widgets()
        self._load_catalog()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_vars(self):
        self.var_name = tk.StringVar()
        self.var_id = tk.StringVar()
        # Holds either a sonar recording or an already-extracted depth CSV.
        self.var_depth_input = tk.StringVar()
        self.var_depth_note = tk.StringVar(value='')
        self.var_csv = tk.StringVar()          # the CSV actually fed to the pipeline
        self.var_temp = tk.StringVar(value=str(DEFAULT_WATER_TEMP))
        self.var_auto_depth = tk.BooleanVar(value=False)
        # Which layers to generate locally from the recording.
        self.var_make_sonar = tk.BooleanVar(value=False)
        self.var_make_substrate = tk.BooleanVar(value=False)
        self.var_make_rock = tk.BooleanVar(value=False)
        self.var_make_ghost = tk.BooleanVar(value=False)
        # How each stage is run: mosaic tone, which pings the imagery keeps,
        # how substrate is classified, how finely rock is predicted. The
        # values live here; the buttons only show which preset they amount to.
        self.processing = default_processing()
        self.var_choice = {group: tk.StringVar(value=describe_choice(
            group, self.processing[group])) for group in PARAMS}
        self.var_substrate = tk.StringVar()
        self.var_zoom = tk.StringVar(value=str(DEFAULT_ZOOM))
        self.var_tz = tk.StringVar(value=DEFAULT_TIMEZONE)
        self.var_lat = tk.StringVar()
        self.var_lon = tk.StringVar()
        self.var_zoom_min = tk.StringVar(value='12')
        self.var_zoom_max = tk.StringVar(value='17')
        self.var_default = tk.BooleanVar(value=False)
        self.var_installed = tk.StringVar(value='')
        self.var_status = tk.StringVar(value='')
        self.var_tide_mode = tk.StringVar(value='none')
        self.var_tide_key = tk.StringVar(value=os.environ.get('WORLDTIDES_API_KEY', ''))
        self.var_tide_info = tk.StringVar(value='')
        # Fitted constants for the entry being edited, kept out of the form fields.
        self.tide_constituents = []

    def _build_widgets(self):
        outer = ttk.Frame(self, padding=8)
        outer.pack(fill='both', expand=True)
        panes = ttk.PanedWindow(outer, orient='horizontal')
        panes.pack(fill='both', expand=True)

        left = ttk.Frame(panes, padding=(0, 0, 8, 0))
        right = ttk.Frame(panes)
        panes.add(left, weight=0)
        panes.add(right, weight=1)

        ttk.Label(left, text="Locations in the app").pack(anchor='w')
        self.listbox = tk.Listbox(left, width=30, height=18, exportselection=False)
        self.listbox.pack(fill='both', expand=True, pady=(2, 6))
        self.listbox.bind('<<ListboxSelect>>', self._on_select)
        btns = ttk.Frame(left)
        btns.pack(fill='x')
        ttk.Button(btns, text="Add", command=self._add).pack(side='left', fill='x', expand=True)
        ttk.Button(btns, text="Remove", command=self._remove).pack(side='left', fill='x', expand=True, padx=(4, 0))
        ttk.Button(btns, text="Combine...", command=self._combine).pack(
            side='left', fill='x', expand=True, padx=(4, 0))
        ttk.Button(btns, text="Merge...", command=self._merge).pack(
            side='left', fill='x', expand=True, padx=(4, 0))

        det = ttk.LabelFrame(right, text="Location", padding=10)
        det.pack(fill='both', expand=True)

        r = 0
        ttk.Label(det, text="Name").grid(row=r, column=0, sticky='w')
        e_name = ttk.Entry(det, textvariable=self.var_name)
        e_name.grid(row=r, column=1, columnspan=2, sticky='ew', pady=2)
        e_name.bind('<KeyRelease>', self._on_name_typed)

        r += 1
        ttk.Label(det, text="Id").grid(row=r, column=0, sticky='w')
        ttk.Entry(det, textvariable=self.var_id).grid(row=r, column=1, columnspan=2, sticky='ew', pady=2)

        # ── local input files ──
        r += 1
        src = ttk.LabelFrame(det, text="Local files for this survey", padding=8)
        src.grid(row=r, column=0, columnspan=3, sticky='ew', pady=(10, 6))

        ttk.Label(src, text="Depth recording").grid(row=0, column=0, sticky='w')
        ttk.Entry(src, textvariable=self.var_depth_input).grid(row=0, column=1, sticky='ew', pady=2)
        ttk.Button(src, text="...", width=3, command=self._pick_depth_input).grid(row=0, column=2, padx=(4, 0))
        ttk.Label(src, textvariable=self.var_depth_note, foreground='#666666',
                  wraplength=430, justify='left').grid(
            row=1, column=1, columnspan=2, sticky='w')

        gen = ttk.LabelFrame(src, text="Generate locally from the recording", padding=6)
        gen.grid(row=2, column=0, columnspan=3, sticky='ew', pady=(6, 6))
        layers = ttk.Frame(gen)
        layers.pack(fill='x')
        ttk.Checkbutton(layers, text="Depth map (always)", state='disabled',
                        variable=tk.BooleanVar(value=True)).pack(side='left')
        ttk.Checkbutton(layers, text="Side scan mosaic", variable=self.var_make_sonar,
                        command=self._on_generate_toggled).pack(side='left', padx=(12, 0))
        ttk.Checkbutton(layers, text="Substrate map", variable=self.var_make_substrate,
                        command=self._on_generate_toggled).pack(side='left', padx=(12, 0))
        ttk.Checkbutton(layers, text="Rock map", variable=self.var_make_rock,
                        command=self._on_generate_toggled).pack(side='left', padx=(12, 0))
        ttk.Checkbutton(layers, text="Bottom objects", variable=self.var_make_ghost,
                        command=self._on_generate_toggled).pack(side='left', padx=(12, 0))
        ttk.Label(layers, text="(slow: minutes)", foreground='#996600').pack(
            side='left', padx=(10, 0))

        # How each stage is run. These are PINGMapper's and RockMapper's own
        # choices, left at their tools' defaults until someone makes them: a
        # mosaic with no corrections keeps the nadir ribbon and mismatched
        # passes, and nothing downstream can put that back. Each button opens
        # the settings behind it, because a name in a dropdown cannot say what
        # EGN is or what to set a clip limit to.
        def setting_row(parent, groups, pad_top):
            row = ttk.Frame(parent)
            row.pack(fill='x', pady=(pad_top, 0))
            for i, group in enumerate(groups):
                ttk.Label(row, text=GROUP_TITLES[group]).pack(
                    side='left', padx=((12 if i else 0), 4))
                ttk.Button(row, textvariable=self.var_choice[group], width=34,
                           command=lambda g=group: self._open_settings(g)).pack(
                    side='left')

        setting_row(gen, ['mosaic', 'track'], 6)
        setting_row(gen, ['substrate', 'rock'], 4)
        setting_row(gen, ['ghost'], 4)

        ttk.Label(src, text="Substrate map").grid(row=3, column=0, sticky='w')
        ttk.Entry(src, textvariable=self.var_substrate).grid(row=3, column=1, sticky='ew', pady=2)
        ttk.Button(src, text="...", width=3, command=self._pick_substrate).grid(row=3, column=2, padx=(4, 0))

        ttk.Label(src, text="Sonar mosaic\n(one per line)").grid(row=4, column=0, sticky='nw', pady=(6, 0))
        self.txt_sonar = tk.Text(src, height=4, wrap='none')
        self.txt_sonar.grid(row=4, column=1, sticky='ew', pady=(6, 2))
        ttk.Button(src, text="...", width=3, command=self._pick_sonar).grid(row=4, column=2, sticky='n', padx=(4, 0), pady=(6, 0))
        src.columnconfigure(1, weight=1)

        # ── build options ──
        r += 1
        opts = ttk.Frame(det)
        opts.grid(row=r, column=0, columnspan=3, sticky='ew')
        ttk.Label(opts, text="Tile zooms").pack(side='left')
        ttk.Entry(opts, textvariable=self.var_zoom_min, width=4).pack(side='left', padx=(4, 2))
        ttk.Label(opts, text="to").pack(side='left')
        ttk.Entry(opts, textvariable=self.var_zoom_max, width=4).pack(side='left', padx=(2, 12))
        ttk.Label(opts, text="Timezone").pack(side='left')
        ttk.Entry(opts, textvariable=self.var_tz, width=20).pack(side='left', padx=(4, 12))
        ttk.Label(opts, text="Water °C").pack(side='left')
        ttk.Entry(opts, textvariable=self.var_temp, width=5).pack(side='left', padx=(4, 12))
        ttk.Checkbutton(opts, text="Model bed pick",
                        variable=self.var_auto_depth).pack(side='left')

        r += 1
        actions = ttk.Frame(det)
        actions.grid(row=r, column=0, columnspan=3, sticky='ew', pady=(10, 4))
        ttk.Button(actions, text="Build charts",
                   command=self._build_and_install).pack(side='left')
        ttk.Button(actions, text="Make chart bundle",
                   command=self._make_bundle).pack(side='left', padx=(6, 0))
        ttk.Label(det, textvariable=self.var_installed, foreground='#336633',
                  wraplength=620, justify='left').grid(row=r + 1, column=0, columnspan=3, sticky='w')

        # ── map placement ──
        r += 2
        place = ttk.Frame(det)
        place.grid(row=r, column=0, columnspan=3, sticky='w', pady=(10, 0))
        ttk.Label(place, text="Pin / camera  lat").pack(side='left')
        ttk.Entry(place, textvariable=self.var_lat, width=12).pack(side='left', padx=(4, 8))
        ttk.Label(place, text="lon").pack(side='left')
        ttk.Entry(place, textvariable=self.var_lon, width=12).pack(side='left', padx=(4, 8))
        ttk.Label(place, text="zoom").pack(side='left')
        ttk.Entry(place, textvariable=self.var_zoom, width=5).pack(side='left', padx=(4, 0))

        # ── tide ──
        r += 1
        tide = ttk.LabelFrame(det, text="Tide", padding=8)
        tide.grid(row=r, column=0, columnspan=3, sticky='ew', pady=(10, 0))
        for text, value in (("No tide (lake, reservoir, river)", 'none'),
                            ("Gulf of California / Guaymas constants", 'guaymas'),
                            ("Fitted constants for this location", 'constituents')):
            ttk.Radiobutton(tide, text=text, value=value,
                            variable=self.var_tide_mode,
                            command=self._commit_form).pack(anchor='w')
        key_row = ttk.Frame(tide)
        key_row.pack(fill='x', pady=(6, 0))
        ttk.Button(key_row, text="Detect from lat/lon",
                   command=self._detect_from_latlon).pack(side='left')
        ttk.Label(key_row, text="WorldTides key").pack(side='left', padx=(12, 4))
        ttk.Entry(key_row, textvariable=self.var_tide_key, show='*', width=24).pack(side='left')
        ttk.Label(tide, textvariable=self.var_tide_info, foreground='#666666',
                  wraplength=600, justify='left').pack(anchor='w', pady=(6, 0))

        r += 1
        ttk.Checkbutton(det, text="Open this location when the app starts",
                        variable=self.var_default, command=self._set_default).grid(
            row=r, column=0, columnspan=3, sticky='w', pady=(8, 0))

        det.columnconfigure(1, weight=1)

        bar = ttk.Frame(outer)
        bar.pack(fill='x', pady=(8, 0))
        ttk.Label(bar, text=f"Catalog: {CATALOG_PATH}", foreground='#666666').pack(side='left')
        ttk.Label(bar, textvariable=self.var_status).pack(side='left', padx=(12, 0))
        ttk.Button(bar, text="Save locations.json", command=self._save).pack(side='right')
        ttk.Button(bar, text="Reload", command=self._load_catalog).pack(side='right', padx=(0, 6))

    # ── catalog ──────────────────────────────────────────────────────────────
    def _load_catalog(self):
        self.entries = []
        self.default_id = None
        if os.path.isfile(CATALOG_PATH):
            try:
                with open(CATALOG_PATH, encoding='utf-8') as f:
                    data = json.load(f)
                self.entries = [self._normalize(e) for e in data.get('locations', [])]
                self.default_id = data.get('defaultLocationId')
            except (OSError, ValueError) as exc:
                messagebox.showerror("Locations", f"Could not read locations.json:\n{exc}")
        if not self.entries:
            # First run: seed with the survey that is already bundled.
            self.entries = [self._normalize(LEGACY_ENTRY)]
            self.default_id = LEGACY_ENTRY['id']
        self.current = None
        self._refresh_list(select=0)
        self._set_dirty(False)

    @staticmethod
    def _normalize(e):
        entry = blank_entry(e.get('name', 'Unnamed'))
        entry.update({k: v for k, v in e.items() if k in entry})
        # blank_entry() has no pack fields - a survey only gets them once it is
        # installed - so carry them across explicitly. Losing them would tell the
        # app an on-demand survey ships inside the APK, where it is not.
        for key in ('pack', 'approxMb'):
            if e.get(key):
                entry[key] = e[key]
        inputs = e.get('inputs') or {}
        entry['inputs'] = {
            'recording': inputs.get('recording', ''),
            'csv': inputs.get('csv', ''),
            'substrate': inputs.get('substrate', ''),
            'sonar': list(inputs.get('sonar') or []),
        }
        entry['generate'] = dict({'sonar': False, 'substrate': False,
                                  'rock': False, 'ghost': False},
                                 **(e.get('generate') or {}))
        entry['processing'] = processing_of(e)
        entry['tiles'] = dict(e.get('tiles') or {})
        entry['grids'] = dict(e.get('grids') or {})
        # No tide block means no tide, matching the app's default.
        entry['tide'] = dict(e['tide']) if 'tide' in e else {'mode': 'none'}
        return entry

    def _save(self):
        self._commit_form()
        for entry in self.entries:
            if not entry['center']:
                messagebox.showerror(
                    "Locations",
                    f"'{entry['name']}' has no pin position yet - build/install it, "
                    "or type a lat/lon.")
                return
        data = {
            'version': CATALOG_VERSION,
            'defaultLocationId': self.default_id or self.entries[0]['id'],
            'locations': self.entries,
        }
        os.makedirs(OUTPUT_ROOT, exist_ok=True)
        with open(CATALOG_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write('\n')
        self._set_dirty(False)
        self.var_status.set(f"Saved {len(self.entries)} location(s)")

    # ── list ─────────────────────────────────────────────────────────────────
    def _refresh_list(self, select=None):
        self.listbox.delete(0, 'end')
        for entry in self.entries:
            mark = '  (start)' if entry['id'] == self.default_id else ''
            self.listbox.insert('end', entry['name'] + mark)
        if self.entries and select is not None:
            index = max(0, min(select, len(self.entries) - 1))
            self.listbox.selection_clear(0, 'end')
            self.listbox.selection_set(index)
            self._load_form(index)

    def _on_select(self, _event=None):
        sel = self.listbox.curselection()
        if not sel or sel[0] == self.current:
            return
        self._commit_form()
        self._load_form(sel[0])

    def _add(self):
        self._commit_form()
        self.entries.append(blank_entry())
        self.current = None
        self._refresh_list(select=len(self.entries) - 1)
        self._set_dirty(True)

    def _combine(self):
        """Fold two or more surveys into a single one covering all of them."""
        self._commit_form()
        built = [e for e in self.entries if resolve_input(e['inputs'].get('csv'))]
        if len(built) < 2:
            messagebox.showerror(
                "Combine surveys",
                "At least two surveys need depth soundings on disk before they can "
                "be combined. Build them first.")
            return
        SurveyPickerDialog(
            self, built, self._on_combined,
            title="Surveys to combine",
            blurb="Their soundings are pooled and their mosaics and rasters merged, "
                  "then Build makes one chart out of the lot - a single depth grid "
                  "with contours running across the join. Slower, but it re-derives "
                  "everything from the raw survey data.",
            action="Combine", suffix="combined")

    def _on_combined(self, entries, name):
        """Create the combined entry, then let the user Build it like any other."""
        def work(log, cancel):
            return combine_entries(entries, name, OUTPUT_ROOT, log)

        def done(entry):
            if not entry:
                return
            self.entries.append(entry)
            self.current = None
            self._refresh_list(select=len(self.entries) - 1)
            self._set_dirty(True)
            self.var_status.set(
                f"Combined {len(entries)} surveys - press Build + install into app")

        TaskDialog(self, f"Combining into {name}", work, done)

    def _built_asset_dirs(self, entry):
        """
        Every folder this survey's built charts might sit in, in the order worth
        trying. Driven by the catalog rather than the id: a renamed survey keeps
        its pipeline output under the old name, and its pack module keeps the
        name it was created with.
        """
        entry_id = entry['id']
        # "creve-coeur-lake1/bathymetry.mbtiles" -> "creve-coeur-lake1"; a flat
        # name (the bundled survey) means the assets root itself.
        subdirs = []
        for path in (entry.get('tiles') or {}).values():
            folder = os.path.dirname(path or '')
            if folder not in subdirs:
                subdirs.append(folder)
        subdirs = subdirs or ['']

        dirs = [os.path.join(OUTPUT_ROOT, entry_id)]
        dirs += [os.path.join(OUTPUT_ROOT, part) for part in subdirs if part]
        return dirs

    def _merge(self):
        """Show several finished surveys together, as one entry with one pin."""
        self._commit_form()
        built = [e for e in self.entries
                 if merge_locations.survey_files(self._built_asset_dirs(e))]
        if len(built) < 2:
            messagebox.showerror(
                "Merge surveys",
                "At least two surveys need finished charts before they can be "
                "merged. Build them first.")
            return
        SurveyPickerDialog(
            self, built, self._on_merged,
            title="Surveys to merge",
            blurb="Their finished charts are stitched into one survey: the tiles, "
                  "contours, shallow bands and query grids of each, under a single "
                  "pin. Nothing is re-tiled, so this takes seconds and each survey "
                  "keeps the detail it was built with.",
            action="Merge", suffix="merged")

    def _on_merged(self, entries, name):
        entry_id = slugify(name)
        out_dir = self._out_dir(entry_id)
        ids = [e['id'] for e in entries]
        folders = [merge_locations.survey_files(self._built_asset_dirs(e)) for e in entries]

        def work(log, cancel):
            made = merge_locations.merge_surveys(folders, name, out_dir, log)
            log("Installing into the app ...")
            result = install_outputs(out_dir, entry_id, log)
            result['merged'] = made
            return result

        def done(result):
            if not result:
                return
            merged = blank_entry(name)
            first = entries[0]
            merged.update({
                'id': entry_id,
                'timezone': first.get('timezone', DEFAULT_TIMEZONE),
                'waterTempC': first.get('waterTempC', DEFAULT_WATER_TEMP),
                'tide': dict(first.get('tide') or {'mode': 'none'}),
                'zoom': first.get('zoom', DEFAULT_ZOOM),
                'tiles': result['tiles'],
                'grids': result['grids'],
                'contours': result.get('contours'),
                'shallowBands': result.get('shallowBands'),
                'mergedFrom': ids,
            })
            if result.get('center'):
                merged['center'] = result['center']
            if result.get('pack'):
                merged['pack'] = result['pack']
                merged['approxMb'] = result.get('approxMb', 0)
            self.entries.append(merged)
            self.current = None
            self._refresh_list(select=len(self.entries) - 1)
            self._set_dirty(True)
            self.var_status.set(
                f"Merged {len(ids)} surveys - save locations.json, then rebuild the app")

        TaskDialog(self, f"Merging into {name}", work, done)

    def _remove(self):
        if self.current is None:
            return
        entry = self.entries[self.current]
        if not messagebox.askokcancel(
                "Locations",
                f"Remove '{entry['name']}' from the app?\n\n"
                "Its installed assets are deleted too."):
            return
        self._delete_installed(entry['id'])
        if entry['id'] == self.default_id:
            self.default_id = None
        del self.entries[self.current]
        index = min(self.current, len(self.entries) - 1) if self.entries else None
        self.current = None
        self._refresh_list(select=index)
        self._set_dirty(True)

    # ── form ─────────────────────────────────────────────────────────────────
    def _open_settings(self, group):
        """The settings behind one button, and what to do with the answer."""
        def taken(choice):
            self.processing[group] = choice
            self.var_choice[group].set(describe_choice(group, choice))
            self._commit_form()

        ProcessingDialog(self, group, self.processing[group], taken)

    def _load_form(self, index):
        self.current = index
        e = self.entries[index]
        self.var_name.set(e['name'])
        self.var_id.set(e['id'])
        self.var_depth_input.set(e['inputs'].get('recording') or e['inputs'].get('csv', ''))
        self.var_temp.set(str(e.get('waterTempC', DEFAULT_WATER_TEMP)))
        self.var_auto_depth.set(bool(e.get('autoDepthPick', False)))
        gen = e.get('generate') or {}
        self.var_make_sonar.set(bool(gen.get('sonar')))
        self.var_make_substrate.set(bool(gen.get('substrate')))
        self.var_make_rock.set(bool(gen.get('rock')))
        self.var_make_ghost.set(bool(gen.get('ghost')))
        self.processing = processing_of(e)
        for group, choice in self.processing.items():
            self.var_choice[group].set(describe_choice(group, choice))
        self._refresh_depth_note()
        self.var_substrate.set(e['inputs'].get('substrate', ''))
        self.txt_sonar.delete('1.0', 'end')
        self.txt_sonar.insert('1.0', '\n'.join(e['inputs'].get('sonar', [])))
        center = e.get('center') or {}
        self.var_lat.set('' if center.get('lat') is None else str(center['lat']))
        self.var_lon.set('' if center.get('lon') is None else str(center['lon']))
        self.var_zoom.set(str(e.get('zoom', DEFAULT_ZOOM)))
        self.var_tz.set(e.get('timezone', DEFAULT_TIMEZONE))
        tide = e.get('tide') or {}
        mode = tide.get('mode', 'none')
        if mode == 'constituents' and not tide.get('constituents'):
            mode = 'none'
        self.var_tide_mode.set(mode)
        self.tide_constituents = list(tide.get('constituents') or [])
        self._refresh_tide_info()
        self.var_default.set(e['id'] == self.default_id)
        self._refresh_installed_label()

    def _commit_form(self):
        if self.current is None or self.current >= len(self.entries):
            return
        e = self.entries[self.current]
        before = json.dumps(e, sort_keys=True)
        e['name'] = self.var_name.get().strip() or e['name']
        new_id = slugify(self.var_id.get() or e['name'])
        if new_id != e['id']:
            self._rename_installed(e, new_id)
        depth_input = self.var_depth_input.get().strip()
        prev_inputs = e.get('inputs') or {}
        prev_csv = prev_inputs.get('csv', '')
        e['inputs'] = {
            # A recording is decoded on Build; a CSV is used as it stands. When a
            # recording is picked, keep the CSV it last decoded to as provenance.
            'recording': depth_input if is_recording(depth_input) else '',
            'csv': prev_csv if is_recording(depth_input) else depth_input,
            # While a layer is generated the picker holds a placeholder, so keep
            # whatever was picked before instead of throwing those paths away.
            'substrate': (prev_inputs.get('substrate', '') if self.var_make_substrate.get()
                          else self.var_substrate.get().strip()),
            'sonar': (list(prev_inputs.get('sonar') or []) if self.var_make_sonar.get() else
                      [s.strip() for s in self.txt_sonar.get('1.0', 'end').splitlines()
                       if s.strip() and not s.strip().startswith('(generated')]),
        }
        lat, lon = self._as_float(self.var_lat), self._as_float(self.var_lon)
        e['center'] = {'lat': lat, 'lon': lon} if lat is not None and lon is not None else None
        e['zoom'] = self._as_float(self.var_zoom) or DEFAULT_ZOOM
        e['timezone'] = self.var_tz.get().strip() or DEFAULT_TIMEZONE
        e['waterTempC'] = self._as_float(self.var_temp) or DEFAULT_WATER_TEMP
        e['autoDepthPick'] = bool(self.var_auto_depth.get())
        e['generate'] = {'sonar': bool(self.var_make_sonar.get()),
                         'substrate': bool(self.var_make_substrate.get()),
                         'rock': bool(self.var_make_rock.get()),
                         'ghost': bool(self.var_make_ghost.get())}
        e['processing'] = {group: {'preset': choice['preset'],
                                   'values': dict(choice['values'])}
                           for group, choice in self.processing.items()}
        e.pop('mosaic', None)          # the shape this setting had first
        mode = self.var_tide_mode.get()
        if mode == 'constituents' and self.tide_constituents:
            e['tide'] = {'mode': 'constituents', 'constituents': self.tide_constituents}
        elif mode == 'guaymas':
            e['tide'] = {'mode': 'guaymas'}   # the app's built-in default
        else:
            e['tide'] = {'mode': 'none'}
        if self.var_default.get():
            self.default_id = e['id']
        if json.dumps(e, sort_keys=True) != before:
            self._set_dirty(True)
            self.listbox.delete(self.current)
            mark = '  (start)' if e['id'] == self.default_id else ''
            self.listbox.insert(self.current, e['name'] + mark)
            self.listbox.selection_set(self.current)

    @staticmethod
    def _as_float(var):
        try:
            return float(var.get().strip())
        except ValueError:
            return None

    def _on_name_typed(self, _event=None):
        if self.current is None:
            return
        e = self.entries[self.current]
        if self.var_id.get() == e['id'] == slugify(e['name']):
            self.var_id.set(slugify(self.var_name.get()))

    def _refresh_tide_info(self):
        mode = self.var_tide_mode.get()
        if mode == 'none':
            self.var_tide_info.set(
                "Depths are shown exactly as surveyed; the app hides the tide readout.")
        elif mode == 'guaymas':
            self.var_tide_info.set(
                "Uses the built-in Gulf of California constants - only right near Santa Rosalía.")
        else:
            n = len(self.tide_constituents)
            self.var_tide_info.set(
                f"{n} fitted constituents stored in the catalog." if n
                else "No constants yet - hit Detect from lat/lon with a WorldTides key.")

    def _detect_from_latlon(self):
        """Fill in timezone (and tide constants when possible) from the coordinates."""
        self._commit_form()
        lat, lon = self._as_float(self.var_lat), self._as_float(self.var_lon)
        if lat is None or lon is None:
            messagebox.showerror(
                "Locations",
                "No coordinates yet - build/install this survey first, or type a lat/lon.")
            return
        api_key = self.var_tide_key.get().strip()

        def work(log, _cancel):
            log(f"Coordinates: {lat}, {lon}\n")
            log("Timezone:")
            tz = timezone_for(lat, lon, log)
            log("\nTide:")
            tide = tide_for(lat, lon, api_key, log)
            return tz, tide

        def done(result):
            if not result:
                return
            tz, tide = result
            self.var_tz.set(tz)
            if tide.get('mode') == 'constituents':
                self.tide_constituents = tide['constituents']
                self.var_tide_mode.set('constituents')
            else:
                self.tide_constituents = []
                self.var_tide_mode.set('none')
            self._refresh_tide_info()
            self._commit_form()
            self._set_dirty(True)
            self.var_status.set(f"Timezone {tz}, tide '{self.var_tide_mode.get()}'")

        TaskDialog(self, "Detecting from lat/lon", work, done)

    def _set_default(self):
        self._commit_form()
        if self.current is None:
            return
        self.default_id = self.entries[self.current]['id'] if self.var_default.get() else None
        self._set_dirty(True)
        self._refresh_list(select=self.current)

    def _set_dirty(self, dirty):
        self.dirty = dirty
        self.title("AnchorHold - Add Survey Locations" + (" *" if dirty else ""))

    # ── file pickers ─────────────────────────────────────────────────────────
    def _pick_depth_input(self):
        # The recordings folder if one is set, so this opens where the
        # cards were copied rather than at the repo.
        path = filedialog.askopenfilename(
            title="Sonar recording (.DAT) or depth CSV",
            initialdir=workspace.open_dir() or repo_dir,
            filetypes=DEPTH_INPUT_FILETYPES)
        if path:
            self.var_depth_input.set(path)
            self._commit_form()
            self._refresh_depth_note()

    def _on_generate_toggled(self):
        """Generated layers come from the recording, so their pickers stand down."""
        # RockMapper needs a side scan mosaic. If files are already attached it
        # runs on those; only with nothing to work from does it imply making one.
        if self.var_make_rock.get() and not self._attached_sonar():
            self.var_make_sonar.set(True)
        self._commit_form()
        self._refresh_depth_note()   # ticking a layer can mean another PINGMapper run
        if self.var_make_sonar.get():
            self.txt_sonar.delete('1.0', 'end')
            self.txt_sonar.insert('1.0', '(generated from the recording on Build)')
            self.txt_sonar.configure(state='disabled')
        else:
            self.txt_sonar.configure(state='normal')
            if '(generated' in self.txt_sonar.get('1.0', 'end'):
                self.txt_sonar.delete('1.0', 'end')
        if self.var_make_substrate.get():
            self.var_substrate.set('(generated from the recording on Build)')
        elif self.var_substrate.get().startswith('(generated'):
            self.var_substrate.set('')

    def _attached_sonar(self):
        """Mosaic files picked by hand for this survey, placeholder text aside."""
        lines = [s.strip() for s in self.txt_sonar.get('1.0', 'end').splitlines()]
        picked = [s for s in lines if s and not s.startswith('(generated')]
        if picked or self.current is None:
            return picked
        # While generation is on, the box holds the placeholder and the real
        # paths live on the entry.
        return list((self.entries[self.current].get('inputs') or {}).get('sonar') or [])

    def _trim_note(self, path: str) -> str:
        """One sentence when a Fixer edit is sitting beside the recording."""
        found = time_filter_for(path)
        if not found:
            return ''
        return (f"  Trimmed by {os.path.basename(found)} - only the stretches"
                " it names will be read.")

    def _refresh_depth_note(self):
        """
        Explain what will happen with whatever was picked.

        A time filter beside the recording is applied on Build without being
        asked for, so it has to be visible before Build is pressed.
        """
        self._depth_note_base()
        extra = self._trim_note(self.var_depth_input.get().strip())
        if extra:
            self.var_depth_note.set(self.var_depth_note.get() + extra)

    def _depth_note_base(self):
        """What the note said before a filter could be found."""
        path = self.var_depth_input.get().strip()
        if not path:
            self.var_depth_note.set(
                "Pick the .DAT recording — its depth soundings are read out on Build.")
            return
        if not os.path.isfile(path):
            self.var_depth_note.set(f"Not found: {path}")
            return
        if is_recording(path):
            csv_path = self._recording_csv_path()
            if csv_path and os.path.isfile(csv_path):
                # A cached decode only saves the run if it also holds the layers
                # now ticked; otherwise Build goes back through PINGMapper.
                sonar, substrate = cached_products(csv_path)
                missing = []
                if self.var_make_sonar.get() and not sonar:
                    missing.append("side scan mosaic")
                if self.var_make_substrate.get() and not substrate:
                    missing.append("substrate map")
                if missing:
                    self.var_depth_note.set(
                        "Recording; depths already extracted, but Build re-runs "
                        "PINGMapper for the " + " and ".join(missing) + " (minutes).")
                else:
                    self.var_depth_note.set(
                        f"Recording; depth CSV already extracted "
                        f"({os.path.basename(csv_path)}) and reused on Build.")
            else:
                extra = []
                if self.var_make_sonar.get():
                    extra.append("side scan mosaic")
                if self.var_make_substrate.get():
                    extra.append("substrate map")
                self.var_depth_note.set(
                    "Recording; PINGMapper reads the depths out of it on Build "
                    + ("plus the " + " and ".join(extra) + " (minutes)."
                       if extra else "(seconds - no imagery is exported)."))
        else:
            self.var_depth_note.set("Depth CSV; used as-is.")

    def _recording_csv_path(self):
        """Where this entry's recording gets decoded to."""
        path = self.var_depth_input.get().strip()
        if not is_recording(path) or self.current is None:
            return ''
        project = os.path.splitext(os.path.basename(path))[0]
        return os.path.join(self._out_dir(self.entries[self.current]['id']),
                            'pingmapper', f"{project}_depth.csv")

    def _pick_substrate(self):
        path = filedialog.askopenfilename(
            title="Substrate raster", initialdir=repo_dir,
            filetypes=[("GeoTIFF", "*.tif *.tiff"), ("All files", "*.*")])
        if path:
            self.var_substrate.set(path)
            self._commit_form()

    def _pick_sonar(self):
        paths = filedialog.askopenfilenames(
            title="Sonar mosaic GeoTIFFs", initialdir=repo_dir,
            filetypes=[("GeoTIFF", "*.tif *.tiff"), ("All files", "*.*")])
        if paths:
            self.txt_sonar.delete('1.0', 'end')
            self.txt_sonar.insert('1.0', '\n'.join(paths))
            self._commit_form()

    # ── build / install ──────────────────────────────────────────────────────
    def _out_dir(self, entry_id):
        return os.path.join(OUTPUT_ROOT, entry_id)

    def _build_and_install(self):
        self._commit_form()
        if self.current is None:
            return
        entry = self.entries[self.current]
        recording = entry['inputs'].get('recording', '')
        csv_path = entry['inputs'].get('csv', '')
        if not recording and not os.path.isfile(csv_path):
            messagebox.showerror(
                "Locations",
                "Pick the sonar recording (.DAT) first - the depths, contours and "
                "depth grid all come from it.")
            return
        if recording and not os.path.isfile(recording):
            messagebox.showerror("Locations", f"Recording not found:\n{recording}")
            return

        gen_now = entry.get('generate') or {}
        attached_sonar = list(entry['inputs'].get('sonar') or [])
        if gen_now.get('rock') and not gen_now.get('sonar') and not attached_sonar:
            messagebox.showerror(
                "Locations",
                "The rock map is predicted from the side scan mosaic. Either attach "
                "the mosaic GeoTIFFs, or tick Side scan mosaic to make them from "
                "the recording.")
            return
        missing = [f for f in attached_sonar if not os.path.isfile(f)]
        if missing and not gen_now.get('sonar'):
            messagebox.showerror(
                "Locations",
                "These sonar mosaics are not on disk:" + chr(10) + chr(10)
                + chr(10).join(missing[:6]))
            return

        out_dir = self._out_dir(entry['id'])
        entry_id = entry['id']
        timezone = entry['timezone']
        temp = entry.get('waterTempC', DEFAULT_WATER_TEMP)
        auto_depth = entry.get('autoDepthPick', False)
        zoom_min = self.var_zoom_min.get().strip() or '12'
        zoom_max = self.var_zoom_max.get().strip() or '17'
        sonar = entry['inputs']['sonar']
        substrate = entry['inputs']['substrate'] or ''
        gen = entry.get('generate') or {}
        make_sonar = bool(gen.get('sonar'))
        make_substrate = bool(gen.get('substrate'))
        make_rock = bool(gen.get('rock'))
        make_ghost = bool(gen.get('ghost'))
        processing = processing_of(entry)
        wanted_settings = decode_settings(processing, make_sonar, make_substrate)
        tide = 'guaymas' if (entry.get('tide') or {}).get('mode') == 'guaymas' else 'none'
        # Reuse an already-decoded CSV so a rebuild after tweaking settings is quick.
        cached_csv = self._recording_csv_path()

        def work(log, cancel):
            depth_csv = csv_path
            local_sonar = []
            local_substrate = []
            if recording:
                # An earlier decode only counts if it also produced the layers
                # being asked for now; otherwise PINGMapper runs again.
                cached_sonar, cached_substrate = cached_products(cached_csv)
                # A mosaic toned some other way, or a substrate map classified
                # some other way, is not what is being asked for. Reusing it is
                # how changing a setting and pressing build again looks exactly
                # like the setting doing nothing.
                same_settings = same_processing(
                    cached_decode_settings(cached_csv), wanted_settings,
                    make_sonar, make_substrate)
                reusable = (cached_csv and os.path.isfile(cached_csv)
                            and (not make_sonar or cached_sonar)
                            and (not make_substrate or cached_substrate)
                            and same_settings)
                if reusable:
                    log("Reusing PINGMapper output from an earlier decode:")
                    log("  " + cached_csv)
                    log("")
                    depth_csv = cached_csv
                    local_sonar = cached_sonar
                    local_substrate = cached_substrate
                else:
                    if (cached_sonar or cached_substrate) and not same_settings:
                        log("What is on disk was built with different settings "
                            "- decoding again.")
                    products = run_pingmapper_products(
                        recording, out_dir, log, cancel,
                        temp=temp, auto_depth=auto_depth,
                        sonar_mosaic=make_sonar, substrate_map=make_substrate,
                        processing=processing)
                    depth_csv = products['csv']
                    local_sonar = products['sonar']
                    local_substrate = products['substrate']

            cmd = [sys.executable, '-u', PROCESS_SCRIPT,
                   '--csv', depth_csv,
                   '--out-dir', out_dir,
                   '--zoom-min', zoom_min,
                   '--zoom-max', zoom_max,
                   '--timezone', timezone,
                   '--tide', tide]
            use_sonar = local_sonar if make_sonar else sonar

            rock_raster = ''
            if make_rock:
                if not use_sonar:
                    log("WARNING: RockMapper needs a side scan mosaic - skipping the rock layer.")
                else:
                    where = "generated" if make_sonar else "attached"
                    log(f"Rock map from the {where} side scan mosaic "
                        f"({len(use_sonar)} file(s)).")
                    rock_raster = run_rock_map(use_sonar,
                                               os.path.join(out_dir, 'rockmapper'),
                                               entry_id, log, cancel,
                                               processing=processing)

            # Bottom objects: GhostVision reads the recording itself, so it needs
            # one - a survey built from a depth CSV has nothing for it to look at.
            detections = ''
            if make_ghost:
                if not recording:
                    log("WARNING: bottom objects need the recording itself - "
                        "attach one, or untick it.")
                else:
                    detections = run_ghost_vision(recording, out_dir, entry_id,
                                                  log, cancel, processing=processing)

            use_substrate = (local_substrate[0] if (make_substrate and local_substrate)
                             else substrate)
            if make_sonar and not local_sonar:
                log("WARNING: no sonar mosaic came out of PINGMapper - skipping that layer.")
            if make_substrate and not local_substrate:
                log("WARNING: no substrate raster came out of PINGMapper - skipping that layer.")
            cmd += ['--sonar'] + use_sonar if use_sonar else ['--sonar']
            cmd += ['--substrate', use_substrate]
            if rock_raster:
                cmd += ['--rock', rock_raster]

            log("Building charts: " + " ".join(cmd) + "\n")
            proc = subprocess.Popen(
                cmd, cwd=repo_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, errors='replace')
            try:
                for line in proc.stdout:
                    log(line.rstrip())
                    if cancel.is_set():
                        proc.terminate()
                        raise RuntimeError("Stopped by user")
            finally:
                proc.stdout.close()
            code = proc.wait()
            if code != 0:
                raise RuntimeError(f"Pipeline exited with code {code}")
            log("\nCollecting what was built ...")
            result = install_outputs(out_dir, entry_id, log)
            result['depthCsv'] = depth_csv
            result['generatedSonar'] = use_sonar if make_sonar else None
            result['generatedSubstrate'] = use_substrate if make_substrate else None
            result['rockRaster'] = rock_raster or None
            return result

        TaskDialog(self, f"Building {entry['name']}", work, self._after_install)

    def _make_bundle(self):
        """
        Pack this survey into one file the app can import by itself.

        Building leaves a survey in the builds folder, which only reaches a
        machine that can see that folder. A bundle is one file that travels
        by any means and is added in Settings, Charts on this computer,
        Add from a bundle file.
        """
        self._commit_form()
        if self.current is None:
            return
        entry = self.entries[self.current]
        out_dir = self._out_dir(entry['id'])
        if not os.path.isdir(out_dir):
            if os.path.isdir(OUTPUT_ROOT) and messagebox.askyesno(
                    "Locations",
                    f"No {out_dir}.\n\nBundle {OUTPUT_ROOT} instead?"):
                out_dir = OUTPUT_ROOT
            else:
                messagebox.showerror(
                    "Locations",
                    f"Nothing to bundle: {out_dir} does not exist.")
                return

        entry_id = entry['id']
        name = entry.get('name') or entry_id
        zoom = entry.get('zoom') or 0.0
        tide = entry.get('tide')
        source = entry.get('source')

        def work(log, _cancel):
            return chart_bundle.build(out_dir, loc_id=entry_id, name=name,
                                      zoom=zoom, tide=tide, source=source,
                                      log=log)

        TaskDialog(self, f"Bundling {name}", work, self._after_bundle)

    def _after_bundle(self, path):
        if not path:
            return
        try:
            shown = os.path.relpath(path, repo_dir)
        except ValueError:                       # another drive
            shown = path
        self.var_status.set(f"Bundle written: {shown}")

    def _after_install(self, result):
        """result = dict from install_outputs(): asset paths + derived center."""
        if not result or self.current is None:
            return
        entry = self.entries[self.current]
        entry['tiles'] = result['tiles']
        entry['grids'] = result['grids']
        entry['contours'] = result.get('contours')
        entry['shallowBands'] = result.get('shallowBands')
        if result.get('detections'):
            entry['detections'] = result['detections']
        else:
            entry.pop('detections', None)
        if result.get('preview'):
            entry['preview'] = result['preview']
        if result.get('pack'):
            entry['pack'] = result['pack']
            entry['approxMb'] = result.get('approxMb', 0)
        else:
            entry.pop('pack', None)
            entry.pop('approxMb', None)
        # Remember the CSV the recording decoded to - it is what was actually built from.
        if result.get('depthCsv'):
            entry['inputs']['csv'] = result['depthCsv']
        if result.get('generatedSonar'):
            entry['inputs']['sonar'] = list(result['generatedSonar'])
        if result.get('generatedSubstrate'):
            entry['inputs']['substrate'] = result['generatedSubstrate']
        if result.get('rockRaster'):
            entry['inputs']['rock'] = result['rockRaster']
        center = result.get('center')
        if not center:
            # No depth grid in that output folder - fall back to the survey track.
            csv_path = entry['inputs'].get('csv') or ''
            if os.path.isfile(csv_path):
                center = center_from_csv(csv_path)
        if center and not entry.get('center'):
            entry['center'] = center
            self.var_lat.set(str(center['lat']))
            self.var_lon.set(str(center['lon']))
        self._set_dirty(True)
        self._refresh_installed_label()
        self.var_status.set("Installed - save locations.json, then rebuild the app")

    def _refresh_installed_label(self):
        if self.current is None:
            self.var_installed.set('')
            return
        entry = self.entries[self.current]
        parts = []
        for layer, asset in (entry.get('tiles') or {}).items():
            path = os.path.join(OUTPUT_ROOT, asset.replace('/', os.sep))
            if os.path.isfile(path):
                parts.append(f"{layer} {os.path.getsize(path) / 1e6:.1f} MB")
        grids = entry.get('grids') or {}
        if grids.get('depth'):
            parts.append("depth grid")
        if grids.get('substrate'):
            parts.append("substrate grid")
        if entry.get('contours'):
            parts.append("contours")
        if entry.get('shallowBands'):
            parts.append("shallow bands")
        self.var_installed.set("Installed: " + ", ".join(parts) if parts else "Nothing installed yet")

    def _rename_installed(self, entry, new_id):
        """Keep installed assets next to their location id when the id changes."""
        old_id = entry['id']
        for root in (OUTPUT_ROOT,):
            old_dir, new_dir = os.path.join(root, old_id), os.path.join(root, new_id)
            if os.path.isdir(old_dir) and not os.path.isdir(new_dir):
                shutil.move(old_dir, new_dir)

        def swap(value):
            return value.replace(f"{old_id}/", f"{new_id}/", 1) if value else value

        entry['tiles'] = {k: swap(v) for k, v in (entry.get('tiles') or {}).items()}
        entry['grids'] = {k: swap(v) for k, v in (entry.get('grids') or {}).items()}
        entry['contours'] = swap(entry.get('contours'))
        entry['shallowBands'] = swap(entry.get('shallowBands'))
        entry['id'] = new_id

    def _delete_installed(self, entry_id):
        for root in (OUTPUT_ROOT,):
            target = os.path.join(root, entry_id)
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)

    def _on_close(self):
        self._commit_form()
        if self.dirty:
            answer = messagebox.askyesnocancel(
                "Locations", "Save changes to locations.json before closing?")
            if answer is None:
                return
            if answer:
                self._save()
                if self.dirty:
                    return
        self.destroy()


def install_outputs(out_dir, entry_id, log):
    """
    Record what one pipeline run produced, for the catalog entry.

    process_data.py has already written every file into out_dir, which is
    output/<id>/, and that is where the browser app looks for a survey it
    has not been given yet. Nothing is copied a second time. The return
    shape is unchanged: paths relative to output/, plus the survey centre
    read from the depth grid.
    """
    result = {'tiles': {}, 'grids': {}, 'contours': None, 'shallowBands': None,
              'detections': None, 'preview': {}, 'center': None,
              'pack': None, 'approxMb': 0}
    total = 0

    for name in TILE_FILES:
        src = os.path.join(out_dir, name)
        if not os.path.isfile(src):
            log(f"  (skip {name} - not produced)")
            continue
        size = os.path.getsize(src)
        total += size
        result['tiles'][name.replace('.mbtiles', '')] = f"{entry_id}/{name}"
        log(f"  {entry_id}/{name}  {size / 1e6:.1f} MB")

    for name in GRID_FILES:
        src = os.path.join(out_dir, name)
        if not os.path.isfile(src):
            log(f"  (skip {name} - not produced)")
            continue
        total += os.path.getsize(src)

    if os.path.isfile(os.path.join(out_dir, 'depth_grid.json')):
        result['grids']['depth'] = f"{entry_id}/depth_grid"
        result['center'] = center_from_grid(
            os.path.join(out_dir, 'depth_grid.json'))
    if os.path.isfile(os.path.join(out_dir, 'substrate_grid.json')):
        result['grids']['substrate'] = f"{entry_id}/substrate_grid"
    if os.path.isfile(os.path.join(out_dir, 'contours.geojson')):
        result['contours'] = f"{entry_id}/contours.geojson"
    if os.path.isfile(os.path.join(out_dir, 'shallow_bands.geojson')):
        result['shallowBands'] = f"{entry_id}/shallow_bands.geojson"
    if os.path.isfile(os.path.join(out_dir, 'detections.geojson')):
        result['detections'] = f"{entry_id}/detections.geojson"

    for name in PREVIEW_FILES:
        if not os.path.isfile(os.path.join(out_dir, name)):
            continue
        key = 'boundary' if name.startswith('boundary') else 'track'
        result['preview'][key] = f"{entry_id}/{name}"

    result['approxMb'] = max(1, round(total / 1e6)) if total else 0
    log(f"\nBuilt in:\n  {out_dir}")
    log("Add it to the browser app in Settings > Charts on this computer.")
    return result


if __name__ == '__main__':
    LocationGUI().mainloop()
