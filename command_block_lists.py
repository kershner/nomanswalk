# Command-state names corresponding to nmspy's EnvironmentLocation values.
# The unset None_ value (0) intentionally resolves to UNKNOWN.
COMMAND_STATE_BY_LOCATION_VALUE = {
    0: "UNKNOWN",
    1: "Default",
    2: "SpaceStation",
    3: "PlanetOnFoot",
    4: "PlanetInShip",
    5: "PlanetInVehicle",
    6: "Underwater",
    7: "Cave",
    8: "IndoorInBase",
    9: "Freighter",
    10: "FreighterInternals",
    11: "AbandonedFreighter",
    12: "InFleet",
    13: "InSpaceObject",
    14: "Nexus",
    15: "Anomaly",
}

ON_PLANET_STATES = {
    "PlanetOnFoot",
    "PlanetInShip",
    "PlanetInVehicle",
    "Underwater",
    "Cave",
    "IndoorInBase",
}

# Restrictions shared by states where the player controls the character.
ON_FOOT_BLOCKED_COMMANDS = [
    "land",
    "anomaly",
    "boost",
    "cruise",
]

# Most character-control states block launch. Omit this layer in states where
# launching should remain available.
LAUNCH_BLOCKED_COMMANDS = [
    "launch",
]

# Restrictions shared by states where the player controls a ship.
IN_COCKPIT_BLOCKED_COMMANDS = [
    "sky",
    "jet",
    "walk",
    "coords",
    "ship",
    "pet",
    "dance",
    "sit",
    "selfie",
]

# Add commands here to block them in every planetary environment.
ON_PLANET_BLOCKED_COMMANDS = [
]

# Restrictions shared by stations, space, freighters, and other non-planets.
OFF_PLANET_BLOCKED_COMMANDS = [
    "teleport",
]

# Unknown includes the conservative cockpit baseline plus off-planet rules.
UNKNOWN_BLOCKED_COMMANDS = [
    *IN_COCKPIT_BLOCKED_COMMANDS,
    *OFF_PLANET_BLOCKED_COMMANDS,
]


BLOCKED_COMMANDS_BY_STATE = {
    "Default": [
        *IN_COCKPIT_BLOCKED_COMMANDS,
        *OFF_PLANET_BLOCKED_COMMANDS,
    ],
    "SpaceStation": [
        *ON_FOOT_BLOCKED_COMMANDS,
        *OFF_PLANET_BLOCKED_COMMANDS,
    ],
    "PlanetOnFoot": [
        *ON_FOOT_BLOCKED_COMMANDS,
        *LAUNCH_BLOCKED_COMMANDS,
        *ON_PLANET_BLOCKED_COMMANDS,
    ],
    "PlanetInShip": [
        *IN_COCKPIT_BLOCKED_COMMANDS,
        *ON_PLANET_BLOCKED_COMMANDS,
    ],
    "PlanetInVehicle": [
        *ON_FOOT_BLOCKED_COMMANDS,
        *LAUNCH_BLOCKED_COMMANDS,
        *ON_PLANET_BLOCKED_COMMANDS,
    ],
    "Underwater": [
        *ON_FOOT_BLOCKED_COMMANDS,
        *LAUNCH_BLOCKED_COMMANDS,
        *ON_PLANET_BLOCKED_COMMANDS,
    ],
    "Cave": [
        *ON_FOOT_BLOCKED_COMMANDS,
        *LAUNCH_BLOCKED_COMMANDS,
        *ON_PLANET_BLOCKED_COMMANDS,
    ],
    "IndoorInBase": [
        *ON_FOOT_BLOCKED_COMMANDS,
        *LAUNCH_BLOCKED_COMMANDS,
        *ON_PLANET_BLOCKED_COMMANDS,
    ],
    "Freighter": [
        *ON_FOOT_BLOCKED_COMMANDS,
        *LAUNCH_BLOCKED_COMMANDS,
        *OFF_PLANET_BLOCKED_COMMANDS,
    ],
    "FreighterInternals": [
        *ON_FOOT_BLOCKED_COMMANDS,
        *LAUNCH_BLOCKED_COMMANDS,
        *OFF_PLANET_BLOCKED_COMMANDS,
    ],
    "AbandonedFreighter": [
        *ON_FOOT_BLOCKED_COMMANDS,
        *LAUNCH_BLOCKED_COMMANDS,
        *OFF_PLANET_BLOCKED_COMMANDS,
    ],
    "InFleet": [
        *ON_FOOT_BLOCKED_COMMANDS,
        *LAUNCH_BLOCKED_COMMANDS,
        *OFF_PLANET_BLOCKED_COMMANDS,
    ],
    "InSpaceObject": [
        *ON_FOOT_BLOCKED_COMMANDS,
        *LAUNCH_BLOCKED_COMMANDS,
        *OFF_PLANET_BLOCKED_COMMANDS,
    ],
    "Nexus": [
        *ON_FOOT_BLOCKED_COMMANDS,
        *OFF_PLANET_BLOCKED_COMMANDS,
    ],
    "Anomaly": [
        *ON_FOOT_BLOCKED_COMMANDS,
        *OFF_PLANET_BLOCKED_COMMANDS,
    ],
    "GALAXY_MAP": [
        *UNKNOWN_BLOCKED_COMMANDS,
    ],
    "UNKNOWN": [
        *UNKNOWN_BLOCKED_COMMANDS,
    ],
}


def resolve_command_state(data, fallback="UNKNOWN"):
    """Resolve a state snapshot to one of the command block-list keys."""
    data = data or {}
    game_state = data.get("state")
    if game_state in {"GALAXY_MAP", "UNKNOWN"}:
        return game_state

    environment = data.get("environment") or {}
    location = environment.get("location_stable")
    if location is None:
        location = environment.get("location")
    if location == "None_":
        return "UNKNOWN"
    if location in BLOCKED_COMMANDS_BY_STATE:
        return location

    raw = environment.get("location_stable_raw")
    if raw is None:
        raw = environment.get("location_raw")
    try:
        location = COMMAND_STATE_BY_LOCATION_VALUE.get(int(raw))
    except (TypeError, ValueError):
        location = None

    return location or (fallback if fallback in BLOCKED_COMMANDS_BY_STATE else "UNKNOWN")


def blocked_commands_for_state(state):
    """Return the configured list, failing closed for an unrecognized state."""
    return BLOCKED_COMMANDS_BY_STATE.get(state, BLOCKED_COMMANDS_BY_STATE["UNKNOWN"])
