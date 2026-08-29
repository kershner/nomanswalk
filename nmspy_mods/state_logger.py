# /// script
# dependencies = ["nmspy==170671.5", "pymhf[gui]==0.2.4"]
#
# [tool.pymhf]
# exe = "NMS.exe"
# steam_gameid = 275850
# start_paused = false
#
# [tool.pymhf.gui]
# always_on_top = false
#
# [tool.pymhf.logging]
# log_dir = "."
# log_level = "info"
# window_name_override = "State Logger"
# ///

import ctypes
import math
import time
import traceback

from pymhf import Mod
from pymhf.gui import FLOAT
from pymhf.gui.decorators import STRING

import nmspy.data.types as nms
from nmspy.data.enums import EnvironmentLocation
from nmspy.decorators import on_state_change, on_fully_booted
from nmspy.common import gameData
from nmspy.engine import GetNodeAbsoluteTransMatrix

from shared_state import (
    NMSModState,
    _make_logger,
    _read_enum32,
    _enum_name,
    _str,
    _write_state,
    get_mod_status,
)

_slog = _make_logger("StateDetector", "nms_state_logger.log")

DEFAULT_POLL_INTERVAL = 5.0

NON_PLANET_LOCATIONS = {
    EnvironmentLocation.Enum.SpaceStation,
    EnvironmentLocation.Enum.Freighter,
    EnvironmentLocation.Enum.FreighterInternals,
    EnvironmentLocation.Enum.AbandonedFreighter,
    EnvironmentLocation.Enum.InFleet,
    EnvironmentLocation.Enum.InSpaceObject,
    EnvironmentLocation.Enum.Nexus,
    EnvironmentLocation.Enum.Anomaly,
}

LOCATION_STATES = {
    EnvironmentLocation.Enum.SpaceStation: "SPACE_STATION",
    EnvironmentLocation.Enum.Freighter: "FREIGHTER",
    EnvironmentLocation.Enum.FreighterInternals: "FREIGHTER",
    EnvironmentLocation.Enum.AbandonedFreighter: "ABANDONED_FREIGHTER",
    EnvironmentLocation.Enum.InFleet: "FLEET",
    EnvironmentLocation.Enum.InSpaceObject: "SPACE_OBJECT",
    EnvironmentLocation.Enum.Nexus: "NEXUS",
    EnvironmentLocation.Enum.Anomaly: "ANOMALY",
}

_live_player_ptr = None
_live_player_addr = 0
_live_player_update_count = 0

_live_env_ptr = None
_live_env_addr = 0
_live_env_update_count = 0

_live_game_state_ptr = None
_live_game_state_addr = 0
_live_game_state_update_count = 0


# ---------------------------------------------------------------------------
# Galaxy name table (reality index → name)
# Source: No Man's Sky Wiki: https://nomanssky.fandom.com/wiki/Galaxy
# ---------------------------------------------------------------------------
GALAXY_NAMES: dict[int, str] = {
    0: "Euclid",
    1: "Hilbert Dimension",
    2: "Calypso",
    3: "Hesperius Dimension",
    4: "Hyades",
    5: "Ickjamatew",
    6: "Budullangr",
    7: "Kikolgallr",
    8: "Eltiensleen",
    9: "Eissentam",
    10: "Elkupalos",
    11: "Aptarkaba",
    12: "Ontiniangp",
    13: "Odiwagiri",
    14: "Ogtialabi",
    15: "Muhacksonto",
    16: "Hitonskyer",
    17: "Rerasmutul",
    18: "Isdoraijung",
    19: "Doctinawyra",
    20: "Loychazinq",
    21: "Zukasizawa",
    22: "Ekwathore",
    23: "Yeberhahne",
    24: "Twerbetek",
    25: "Sivarates",
    26: "Eajerandal",
    27: "Aldukesci",
    28: "Wotyarogii",
    29: "Sudzerbal",
    30: "Maupenzhay",
    31: "Sugueziume",
    32: "Brogoweldian",
    33: "Ehbogdenbu",
    34: "Ijsenufryos",
    35: "Nipikulha",
    36: "Autsurabin",
    37: "Lusontrygiamh",
    38: "Rewmanawa",
    39: "Ethiophodhe",
    40: "Urastrykle",
    41: "Xobeurindj",
    42: "Oniijialdu",
    43: "Wucetosucc",
    44: "Ebyeloof",
    45: "Odyavanta",
    46: "Milekistri",
    47: "Waferganh",
    48: "Agnusopwit",
    49: "Teyaypilny",
    50: "Zalienkosm",
    51: "Ladgudiraf",
    52: "Mushonponte",
    53: "Amsentisz",
    54: "Fladiselm",
    55: "Laanawemb",
    56: "Ilkerloor",
    57: "Davanossi",
    58: "Ploehrliou",
    59: "Corpinyaya",
    60: "Leckandmeram",
    61: "Quulngais",
    62: "Nokokipsechl",
    63: "Rinblodesa",
    64: "Loydporpen",
    65: "Ibtrevskip",
    66: "Elkowaldb",
    67: "Heholhofsko",
    68: "Yebrilowisod",
    69: "Husalvangewi",
    70: "Ovna'uesed",
    71: "Bahibusey",
    72: "Nuybeliaure",
    73: "Doshawchuc",
    74: "Ruckinarkh",
    75: "Thorettac",
    76: "Nuponoparau",
    77: "Moglaschil",
    78: "Uiweupose",
    79: "Nasmilete",
    80: "Ekdaluskin",
    81: "Hakapanasy",
    82: "Dimonimba",
    83: "Cajaccari",
    84: "Olonerovo",
    85: "Umlanswick",
    86: "Henayliszm",
    87: "Utzenmate",
    88: "Umirpaiya",
    89: "Paholiang",
    90: "Iaereznika",
    91: "Yudukagath",
    92: "Boealalosnj",
    93: "Yaevarcko",
    94: "Coellosipp",
    95: "Wayndohalou",
    96: "Smoduraykl",
    97: "Apmaneessu",
    98: "Hicanpaav",
    99: "Akvasanta",
    100: "Tuychelisaor",
    101: "Rivskimbe",
    102: "Daksanquix",
    103: "Kissonlin",
    104: "Aediabiel",
    105: "Ulosaginyik",
    106: "Roclaytonycar",
    107: "Kichiaroa",
    108: "Irceauffey",
    109: "Nudquathsenfe",
    110: "Getaizakaal",
    111: "Hansolmien",
    112: "Bloytisagra",
    113: "Ladsenlay",
    114: "Luyugoslasr",
    115: "Ubredhatk",
    116: "Cidoniana",
    117: "Jasinessa",
    118: "Torweierf",
    119: "Saffneckm",
    120: "Thnistner",
    121: "Dotusingg",
    122: "Luleukous",
    123: "Jelmandan",
    124: "Otimanaso",
    125: "Enjaxusanto",
    126: "Sezviktorew",
    127: "Zikehpm",
    128: "Bephembah",
    129: "Broomerrai",
    130: "Meximicka",
    131: "Venessika",
    132: "Gaiteseling",
    133: "Zosakasiro",
    134: "Drajayanes",
    135: "Ooibekuar",
    136: "Urckiansi",
    137: "Dozivadido",
    138: "Emiekereks",
    139: "Meykinunukur",
    140: "Kimycuristh",
    141: "Roansfien",
    142: "Isgarmeso",
    143: "Daitibeli",
    144: "Gucuttarik",
    145: "Enlaythie",
    146: "Drewweste",
    147: "Akbulkabi",
    148: "Homskiw",
    149: "Zavainlani",
    150: "Jewijkmas",
    151: "Itlhotagra",
    152: "Podalicess",
    153: "Hiviusauer",
    154: "Halsebenk",
    155: "Puikitoac",
    156: "Gaybakuaria",
    157: "Grbodubhe",
    158: "Rycempler",
    159: "Indjalala",
    160: "Fontenikk",
    161: "Pasycihelwhee",
    162: "Ikbaksmit",
    163: "Telicianses",
    164: "Oyleyzhan",
    165: "Uagerosat",
    166: "Impoxectin",
    167: "Twoodmand",
    168: "Hilfsesorbs",
    169: "Ezdaranit",
    170: "Wiensanshe",
    171: "Ewheelonc",
    172: "Litzmantufa",
    173: "Emarmatosi",
    174: "Mufimbomacvi",
    175: "Wongquarum",
    176: "Hapirajua",
    177: "Igbinduina",
    178: "Wepaitvas",
    179: "Sthatigudi",
    180: "Yekathsebehn",
    181: "Ebedeagurst",
    182: "Nolisonia",
    183: "Ulexovitab",
    184: "Iodhinxois",
    185: "Irroswitzs",
    186: "Bifredait",
    187: "Beiraghedwe",
    188: "Yeonatlak",
    189: "Cugnatachh",
    190: "Nozoryenki",
    191: "Ebralduri",
    192: "Evcickcandj",
    193: "Ziybosswin",
    194: "Heperclait",
    195: "Sugiuniam",
    196: "Aaseertush",
    197: "Uglyestemaa",
    198: "Horeroedsh",
    199: "Drundemiso",
    200: "Ityanianat",
    201: "Purneyrine",
    202: "Dokiessmat",
    203: "Nupiacheh",
    204: "Dihewsonj",
    205: "Rudrailhik",
    206: "Tweretnort",
    207: "Snatreetze",
    208: "Iwundaracos",
    209: "Digarlewena",
    210: "Erquagsta",
    211: "Logovoloin",
    212: "Boyaghosganh",
    213: "Kuolungau",
    214: "Pehneldept",
    215: "Yevettiiqidcon",
    216: "Sahliacabru",
    217: "Noggalterpor",
    218: "Chmageaki",
    219: "Veticueca",
    220: "Vittesbursul",
    221: "Nootanore",
    222: "Innebdjerah",
    223: "Kisvarcini",
    224: "Cuzcogipper",
    225: "Pamanhermonsu",
    226: "Brotoghek",
    227: "Mibittara",
    228: "Huruahili",
    229: "Raldwicarn",
    230: "Ezdartlic",
    231: "Badesclema",
    232: "Isenkeyan",
    233: "Iadoitesu",
    234: "Yagrovoisi",
    235: "Ewcomechio",
    236: "Inunnunnoda",
    237: "Dischiutun",
    238: "Yuwarugha",
    239: "Ialmendra",
    240: "Reponudrle",
    241: "Rinjanagrbo",
    242: "Zeziceloh",
    243: "Oeileutasc",
    244: "Zicniijinis",
    245: "Dugnowarilda",
    246: "Neuxoisan",
    247: "Ilmenhorn",
    248: "Rukwatsuku",
    249: "Nepitzaspru",
    250: "Chcehoemig",
    251: "Haffneyrin",
    252: "Uliciawai",
    253: "Tuhgrespod",
    254: "Iousongola",
    255: "Odyalutai",
}

def galaxy_name(idx: int) -> str:
    """Return the galaxy name for a reality index, or a fallback string."""
    return GALAXY_NAMES.get(idx, f"Galaxy-{idx}")


def _valid_float(v: float, limit: float = 100_000_000.0) -> bool:
    return math.isfinite(float(v)) and abs(float(v)) < limit


def _matrix_looks_valid(mat) -> bool:
    try:
        vals = (
            float(mat.pos.x),
            float(mat.pos.y),
            float(mat.pos.z),
            float(mat.at.x),
            float(mat.at.y),
            float(mat.at.z),
        )

        if not all(_valid_float(v) for v in vals):
            return False

        pos_nonzero = any(abs(v) > 0.001 for v in vals[:3])
        dir_nonzero = any(abs(v) > 0.001 for v in vals[3:])

        return pos_nonzero and dir_nonzero

    except Exception:
        return False


def _round_pos(pos):
    return {
        "x": round(float(pos.x), 3),
        "y": round(float(pos.y), 3),
        "z": round(float(pos.z), 3),
    }


def _get_live_player():
    try:
        if _live_player_ptr:
            return _live_player_ptr.contents
    except Exception:
        pass

    return None


def _get_live_environment():
    try:
        if _live_env_ptr:
            return _live_env_ptr.contents
    except Exception:
        pass

    try:
        return gameData.player_environment
    except Exception:
        return None


def _get_live_game_state():
    try:
        if _live_game_state_ptr:
            return _live_game_state_ptr.contents
    except Exception:
        pass

    try:
        return gameData.game_state
    except Exception:
        return None


def _get_player_state():
    try:
        if _live_game_state_ptr:
            return _live_game_state_ptr.contents.mPlayerState
    except Exception:
        pass

    try:
        return gameData.player_state
    except Exception:
        return None


def _read_player_position_from_environment(env=None):
    try:
        if env is None:
            env = _get_live_environment()

        if env is None:
            return None

        mat = env.mPlayerTM

        if not _matrix_looks_valid(mat):
            return None

        return _round_pos(mat.pos)

    except Exception:
        return None


def _read_player_position_from_live_player():
    try:
        player = _get_live_player()

        if player is None:
            return None

        mat = GetNodeAbsoluteTransMatrix(player.mRootNode)

        if not _matrix_looks_valid(mat):
            return None

        return _round_pos(mat.pos)

    except Exception:
        return None


def _read_player_position_from_sim():
    pos = _read_player_position_from_environment()

    if pos is not None:
        return pos

    return _read_player_position_from_live_player()


def _state_from_location(loc):
    if loc in (EnvironmentLocation.Enum.PlanetInShip, EnvironmentLocation.Enum.Default):
        return "IN_COCKPIT"

    return LOCATION_STATES.get(loc, "ON_FOOT")


def _is_non_planet_location(env_data):
    loc = env_data.get("location_stable_raw", env_data.get("location_raw", -1))

    try:
        return EnvironmentLocation.Enum(loc) in NON_PLANET_LOCATIONS
    except Exception:
        return False


def _normalize_planet_index(raw_idx):
    if raw_idx is None:
        return -1

    try:
        idx = int(raw_idx)
    except Exception:
        return -1

    if 0 <= idx <= 5:
        return idx

    return -1


def _read_raw_ga_values():
    try:
        ps = _get_player_state()

        if ps is None:
            return None

        loc = ps.mLocation
        ga = loc.GalacticAddress

        return {
            "raw_planet_index": int(ga.PlanetIndex),
            "solar_system_index": int(ga.SolarSystemIndex),
            "voxel_x": int(ga.VoxelX),
            "voxel_y": int(ga.VoxelY),
            "voxel_z": int(ga.VoxelZ),
            "reality_index": int(loc.RealityIndex),
            "source": "live_game_state.mPlayerState.mLocation",
        }

    except Exception:
        _slog.warning("_read_raw_ga_values failed: %s", traceback.format_exc())
        return None


def _read_planet_index_raw():
    vals = _read_raw_ga_values()

    if not vals:
        return -1

    raw = vals["raw_planet_index"]

    if 0 <= raw <= 5:
        return raw

    return -1


def _read_planet_index_from_environment(env=None):
    try:
        if env is None:
            env = _get_live_environment()

        if env is None:
            return -1

        idx = int(env.miNearestPlanetIndex)

        if 0 <= idx <= 5:
            return idx

    except Exception:
        pass

    return -1


def _gather_player_data(current_state):
    try:
        ps = _get_player_state()

        if ps is None:
            return {}

        health = int(ps.miHealth)

        if not (0 <= health < 50_000_000):
            return {}

        result = {
            "name": _str(ps.mNameWithTitle),
            "health": health,
            "shield": max(0, int(ps.miShield)),
            "units": int(ps.muUnits),
            "nanites": int(ps.muNanites),
            "quicksilver": int(ps.muSpecials),
        }

        if current_state == "IN_COCKPIT":
            result["ship_health"] = max(0, int(ps.miShipHealth))

        return result

    except Exception:
        _slog.warning("_gather_player_data failed: %s", traceback.format_exc())
        return {}


def _gather_player_movement():
    try:
        p = _get_live_player()

        if p is None:
            return {}

        stamina = float(p.mfStamina)
        jetpack = float(p.mfJetpackTank)

        if not (0.0 <= stamina <= 1_000_000.0):
            return {}

        if not (-1_000_000.0 <= jetpack <= 1_000_000.0):
            return {}

        return {
            "stamina": round(stamina, 3),
            "jetpack_tank": round(jetpack, 3),
            "is_running": bool(p.mbIsRunning),
            "is_auto_walking": bool(p.mbIsAutoWalking),
            "is_dying": bool(p.mbIsDying),
        }

    except Exception:
        _slog.warning("_gather_player_movement failed: %s", traceback.format_exc())
        return {}


def _gather_universe_address():
    try:
        vals = _read_raw_ga_values()

        if not vals:
            return {}

        raw_pi = vals["raw_planet_index"]
        env_pi = _read_planet_index_from_environment()
        pi = env_pi if 0 <= env_pi <= 5 else _normalize_planet_index(raw_pi)

        si = vals["solar_system_index"]
        vx = vals["voxel_x"]
        vy = vals["voxel_y"]
        vz = vals["voxel_z"]
        ri = vals["reality_index"]

        if not (
            abs(vx) < 5000
            and abs(vz) < 5000
            and abs(vy) <= 5000
            and 0 <= si < 800
            and 0 <= ri <= 255
        ):
            _slog.warning(
                "rejecting universe address source=%s pi=%s si=%s voxel=(%s,%s,%s) reality=%s",
                vals.get("source"),
                raw_pi,
                si,
                vx,
                vy,
                vz,
                ri,
            )
            return {}

        result = {
            "voxel_x": vx,
            "voxel_y": vy,
            "voxel_z": vz,
            "solar_system_index": si,
            "reality_index": ri,
            "galaxy_number": ri + 1,
            "galaxy_name": galaxy_name(ri),
            "source": vals.get("source", "unknown"),
        }

        if 0 <= pi <= 5:
            result["planet_index"] = pi
        if 0 <= raw_pi <= 5:
            result["planet_index_raw"] = raw_pi

        return result

    except Exception:
        _slog.warning("_gather_universe_address failed: %s", traceback.format_exc())
        return {}


def _gather_environment_data(env=None):
    try:
        result = {}

        if env is None:
            env = _get_live_environment()

        pos = _read_player_position_from_environment(env)

        if pos is None:
            pos = _read_player_position_from_live_player()

        if pos is not None:
            result["player_position"] = pos

        env_idx = _read_planet_index_from_environment(env)

        if 0 <= env_idx <= 5:
            result["nearest_planet_index"] = env_idx
            result["nearest_planet_index_raw"] = env_idx
            result["nearest_planet_index_source"] = "player_environment"

        else:
            raw_idx = _read_planet_index_raw()
            idx = _normalize_planet_index(raw_idx)

            if 0 <= idx <= 5:
                result["nearest_planet_index"] = idx
                result["nearest_planet_index_raw"] = raw_idx
                result["nearest_planet_index_source"] = "player_state_location"

        if env is None:
            return result

        loc_val = None
        stable_val = None

        try:
            loc_val = _read_enum32(env.meLocation)
            result["location_raw"] = loc_val
            name = _enum_name(EnvironmentLocation.Enum, loc_val)

            if name and name != str(loc_val):
                result["location"] = name

        except Exception:
            pass

        try:
            stable_val = _read_enum32(env.meLocationStable)
            result["location_stable_raw"] = stable_val
            name = _enum_name(EnvironmentLocation.Enum, stable_val)

            if name and name != str(stable_val):
                result["location_stable"] = name

        except Exception:
            pass

        try:
            result["is_in_cave"] = (
                loc_val == int(EnvironmentLocation.Enum.Cave)
                or stable_val == int(EnvironmentLocation.Enum.Cave)
            )
        except Exception:
            result["is_in_cave"] = False

        try:
            result["distance_from_planet"] = round(float(env.mfDistanceFromPlanet), 2)
        except Exception:
            pass

        try:
            result["nearest_planet_sealevel"] = round(float(env.mfNearestPlanetSealevel), 2)
        except Exception:
            pass

        try:
            result["inside_atmosphere"] = bool(env.mbInsidePlanetAtmosphere)
        except Exception:
            pass

        return result

    except Exception:
        _slog.warning("_gather_environment_data failed: %s", traceback.format_exc())
        return {}


def _gather_planet_data(planet_ptr):
    try:
        if not planet_ptr:
            return {}

        planet = planet_ptr.contents
        pd = planet.mPlanetData
        pgid = planet.mPlanetGenerationInputData
        info = pd.PlanetInfo
        weather_data = pd.Weather
        hazard = pd.Hazard
        name = _str(pd.Name)

        if not name or not name.isprintable():
            return {}

        def _hv(arr, idx):
            try:
                return round(float(arr[idx]), 3)
            except Exception:
                return None

        return {
            "name": name,
            "biome": _enum_name(pgid.Biome.__class__, _read_enum32(pgid.Biome)),
            "planet_size": _enum_name(pgid.PlanetSize.__class__, _read_enum32(pgid.PlanetSize)),
            "has_rings": bool(pd.Rings.HasRings),
            "is_prime": bool(pgid.Prime),
            "in_pirate_system": bool(pgid.InPirateSystem),
            "description": _str(info.PlanetDescription),
            "planet_type": _str(info.PlanetType),
            "weather_label": _str(info.Weather),
            "flora_label": _str(info.Flora),
            "fauna_label": _str(info.Fauna),
            "resources_label": _str(info.Resources),
            "is_extreme_weather": bool(info.IsWeatherExtreme),
            "weather_type": _enum_name(
                weather_data.WeatherType.__class__,
                _read_enum32(weather_data.WeatherType),
            ),
            "weather_intensity": _enum_name(
                weather_data.WeatherIntensity.__class__,
                _read_enum32(weather_data.WeatherIntensity),
            ),
            "storm_frequency": _enum_name(
                weather_data.StormFrequency.__class__,
                _read_enum32(weather_data.StormFrequency),
            ),
            "creature_life": _enum_name(pd.CreatureLife.__class__, _read_enum32(pd.CreatureLife)),
            "life": _enum_name(pd.Life.__class__, _read_enum32(pd.Life)),
            "inhabiting_race": _enum_name(
                pd.InhabitingRace.__class__,
                _read_enum32(pd.InhabitingRace),
            ),
            "sentinel_level": _enum_name(
                pd.GroundCombatDataPerDifficulty[0].SentinelLevel.__class__,
                _read_enum32(pd.GroundCombatDataPerDifficulty[0].SentinelLevel),
            ),
            "hazards": {
                "temperature_ambient": _hv(hazard.Temperature, 0),
                "temperature_storm": _hv(hazard.Temperature, 3),
                "toxicity_ambient": _hv(hazard.Toxicity, 0),
                "toxicity_storm": _hv(hazard.Toxicity, 3),
                "radiation_ambient": _hv(hazard.Radiation, 0),
                "radiation_storm": _hv(hazard.Radiation, 3),
                "life_support_drain": _hv(hazard.LifeSupportDrain, 0),
            },
            "common_substance": _str(pd.CommonSubstanceID),
            "uncommon_substance": _str(pd.UncommonSubstanceID),
            "rare_substance": _str(pd.RareSubstanceID),
            "in_abandoned_system": bool(pd.InAbandonedSystem),
            "in_empty_system": bool(pd.InEmptySystem),
        }

    except Exception:
        _slog.warning("_gather_planet_data failed: %s", traceback.format_exc())
        return {}


def _gather_solar_system_data(planet_ptr):
    try:
        if not planet_ptr:
            return {}

        pgid = planet_ptr.contents.mPlanetGenerationInputData

        return {
            "star_type": _enum_name(pgid.Star.__class__, _read_enum32(pgid.Star)),
        }

    except Exception:
        _slog.warning("_gather_solar_system_data failed: %s", traceback.format_exc())
        return {}


def _find_standing_planet(planet_ptrs: dict) -> int:
    best_idx = -1
    best_radius = 0.0

    for idx, ptr in planet_ptrs.items():
        try:
            name = _str(ptr.contents.mPlanetData.Name)

            if not name:
                continue

            addr = ctypes.cast(ptr, ctypes.c_void_p).value

            if not addr:
                continue

            radius = ctypes.c_float.from_address(addr + 0x3BB0).value

            if radius > best_radius:
                best_radius = radius
                best_idx = idx

        except Exception:
            pass

    if best_radius > 10.0:
        return best_idx

    return -1


def _choose_planet_index(env_data, standing_idx=-1):
    ua = _gather_universe_address()
    env_idx = env_data.get("nearest_planet_index", -1)
    ga_idx = ua.get("planet_index", -1)

    if 0 <= env_idx <= 5:
        return env_idx, ua

    if standing_idx >= 0:
        return standing_idx, ua

    return ga_idx, ua


def _build_full_payload(current_state, env_data, planet_ptrs, standing_idx=-1):
    idx, ua = _choose_planet_index(env_data, standing_idx)
    env_copy = dict(env_data)
    non_planet = _is_non_planet_location(env_copy)

    if non_planet:
        env_copy.pop("nearest_planet_index", None)
        env_copy.pop("nearest_planet_index_raw", None)
        env_copy.pop("nearest_planet_index_source", None)
        env_copy.pop("distance_from_planet", None)
        env_copy.pop("nearest_planet_sealevel", None)
        ua.pop("planet_index", None)
        ua.pop("planet_index_raw", None)
        planet_ptr = None
    else:
        env_copy["nearest_planet_index"] = idx
        planet_ptr = planet_ptrs.get(idx)

    return {
        "state": current_state,
        "player": _gather_player_data(current_state),
        "movement": _gather_player_movement(),
        "universe_address": ua,
        "environment": env_copy,
        "planet": _gather_planet_data(planet_ptr) if planet_ptr else {},
        "solar_system": _gather_solar_system_data(planet_ptr) if planet_ptr else {},
        "mods": get_mod_status(),
    }


class StateLogger(Mod):
    __author__ = "Tyler Kershner"
    __description__ = "State logger"
    __version__ = "1.4-location-context"

    state = NMSModState()

    _last_env_data: dict = {}
    _last_write_time: float = 0.0
    _poll_interval: float = DEFAULT_POLL_INTERVAL
    _planet_ptrs: dict = {}
    _standing_planet_idx: int = -1

    @property
    @STRING("Current State:")
    def current_state(self):
        return self.state.current or "UNKNOWN"

    @current_state.setter
    def current_state(self, value):
        if value != self.state.current:
            self.state.current = value
            self._write_now()

    @property
    @FLOAT("Poll interval (seconds):")
    def poll_interval(self):
        return self._poll_interval

    @poll_interval.setter
    def poll_interval(self, value):
        self._poll_interval = max(1.0, float(value))

    def _write_now(self):
        terrain_idx = _find_standing_planet(self._planet_ptrs)
        ga = _gather_universe_address()
        ga_raw_idx = ga.get("planet_index_raw", -1)
        ga_idx = ga.get("planet_index", -1)
        env_raw_idx = self._last_env_data.get("nearest_planet_index_raw", -1)
        env_idx = self._last_env_data.get("nearest_planet_index", -1)

        _slog.info(
            "PLANET_SELECT terrain_idx=%s standing_idx=%s ga_raw_idx=%s ga_idx=%s "
            "reality=%s galaxy=%s source=%s env_raw_idx=%s env_idx=%s cached=%s "
            "live_env_updates=%s live_game_state_updates=%s live_player_updates=%s "
            "location=%s location_stable=%s is_in_cave=%s",
            terrain_idx,
            self._standing_planet_idx,
            ga_raw_idx,
            ga_idx,
            ga.get("reality_index"),
            ga.get("galaxy_name"),
            ga.get("source"),
            env_raw_idx,
            env_idx,
            sorted(self._planet_ptrs.keys()),
            _live_env_update_count,
            _live_game_state_update_count,
            _live_player_update_count,
            self._last_env_data.get("location_raw"),
            self._last_env_data.get("location_stable_raw"),
            self._last_env_data.get("is_in_cave"),
        )

        _write_state(
            _build_full_payload(
                self.state.current or "UNKNOWN",
                self._last_env_data,
                self._planet_ptrs,
                self._standing_planet_idx,
            )
        )

        self._last_write_time = time.time()

    def _restore_from_location(self):
        self.current_state = _state_from_location(self.state.last_location_stable)

    def _cache_planet(self, this, source: str):
        try:
            if not this:
                return

            idx = int(this.contents.miPlanetIndex)
            name = _str(this.contents.mPlanetData.Name)

            if source == "Construct":
                if idx == 0:
                    self._planet_ptrs.clear()

                existing = self._planet_ptrs.get(idx)

                if existing is not None:
                    try:
                        if _str(existing.contents.mPlanetData.Name):
                            return
                    except Exception:
                        pass

            self._planet_ptrs[idx] = this

        except Exception:
            _slog.warning("_cache_planet source=%s failed: %s", source, traceback.format_exc())

    @nms.cGcGameState.Update.before
    def on_game_state_update(self, this, lfTimeStep):
        global _live_game_state_ptr, _live_game_state_addr, _live_game_state_update_count

        try:
            addr = ctypes.cast(this, ctypes.c_void_p).value or 0

            if not addr:
                return

            _live_game_state_ptr = this
            _live_game_state_addr = addr
            _live_game_state_update_count += 1

        except Exception:
            pass

    @nms.cGcPlayerEnvironment.Update.before
    def on_player_environment_update(self, this, lfTimeStep):
        global _live_env_ptr, _live_env_addr, _live_env_update_count

        try:
            env = this.contents
            mat = env.mPlayerTM

            if not _matrix_looks_valid(mat):
                return

            idx = int(env.miNearestPlanetIndex)

            if not (-1 <= idx <= 5):
                return

            addr = ctypes.cast(this, ctypes.c_void_p).value or 0

            if not addr:
                return

            _live_env_ptr = this
            _live_env_addr = addr
            _live_env_update_count += 1

        except Exception:
            pass

    @nms.cGcPlayer.Update.before
    def on_player_update(self, this, lfStep):
        global _live_player_ptr, _live_player_addr, _live_player_update_count

        try:
            player = this.contents
            mat = GetNodeAbsoluteTransMatrix(player.mRootNode)

            if not _matrix_looks_valid(mat):
                return

            addr = ctypes.cast(this, ctypes.c_void_p).value or 0

            if not addr:
                return

            if _live_player_addr and addr != _live_player_addr:
                existing = _get_live_player()

                if existing is not None:
                    try:
                        existing_mat = GetNodeAbsoluteTransMatrix(existing.mRootNode)

                        if _matrix_looks_valid(existing_mat):
                            return

                    except Exception:
                        pass

            _live_player_ptr = this
            _live_player_addr = addr
            _live_player_update_count += 1

        except Exception:
            pass

    @on_fully_booted
    def on_game_booted(self):
        global _live_player_ptr, _live_player_addr, _live_player_update_count
        global _live_env_ptr, _live_env_addr, _live_env_update_count
        global _live_game_state_ptr, _live_game_state_addr, _live_game_state_update_count

        _live_player_ptr = None
        _live_player_addr = 0
        _live_player_update_count = 0

        _live_env_ptr = None
        _live_env_addr = 0
        _live_env_update_count = 0

        _live_game_state_ptr = None
        _live_game_state_addr = 0
        _live_game_state_update_count = 0

        self.state.current = ""
        self.state.last_location_stable = -1
        self._last_write_time = 0.0
        self._last_env_data = {}
        self._planet_ptrs = {}
        self._standing_planet_idx = -1

    @nms.cGcPlanet.SetupRegionMap.after
    def on_planet_setup(self, this: ctypes._Pointer[nms.cGcPlanet]):
        self._cache_planet(this, "SetupRegionMap")

    @nms.cGcPlanet.Construct.after
    def on_planet_construct(self, this: ctypes._Pointer[nms.cGcPlanet], *args):
        self._cache_planet(this, "Construct")

    @nms.cGcPlanet.Generate.after
    def on_planet_generate(self, this: ctypes._Pointer[nms.cGcPlanet], *args):
        self._cache_planet(this, "Generate")

    @on_state_change("GALAXYMAP")
    def on_enter_galaxy_map(self):
        self.state.in_galaxy_map = True
        self.state.galaxy_map_entered_at = time.time()
        self.current_state = "GALAXY_MAP"

    @nms.cGcApplication.Update.after
    def on_main_loop(self, this):
        try:
            pos = _read_player_position_from_sim()

            if pos is not None:
                self._last_env_data["player_position"] = pos

            pe = _get_live_environment()

            if pe is not None:
                env_data = _gather_environment_data(pe)

                if "player_position" in self._last_env_data and "player_position" not in env_data:
                    env_data["player_position"] = self._last_env_data["player_position"]

                self._last_env_data = env_data

                try:
                    loc_stable = _read_enum32(pe.meLocationStable)

                    if loc_stable != self.state.last_location_stable:
                        self.state.last_location_stable = loc_stable
                        self.state.in_galaxy_map = False

                        self.current_state = _state_from_location(loc_stable)

                except Exception:
                    pass

        except Exception:
            pass

        if time.time() - self._last_write_time >= self._poll_interval:
            self._write_now()
