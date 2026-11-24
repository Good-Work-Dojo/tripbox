# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The shipped lists and the config directory's overrides."""

import os
import signal
from importlib import resources

import pytest

from tripbox import lists
from tripbox.validate.rules import carrier_from_name

# What the lists held as code before they became data files.
DOMAINS = ('aa.com',
 'united.com',
 'delta.com',
 'southwest.com',
 'jetblue.com',
 'alaskaair.com',
 'spirit.com',
 'spirit-airlines.com',
 'flyfrontier.com',
 'allegiantair.com',
 'hawaiianairlines.com',
 'flybreeze.com',
 'aveloair.com',
 'suncountry.com',
 'aircanada.com',
 'aircanada.ca',
 'westjet.com',
 'aeromexico.com',
 'volaris.com',
 'vivaaerobus.com',
 'copaair.com',
 'britishairways.com',
 'virginatlantic.com',
 'airfrance.com',
 'klm.com',
 'lufthansa.com',
 'iberia.com',
 'aerlingus.com',
 'icelandair.com',
 'emirates.com',
 'qatarairways.com',
 'latam.com',
 'avianca.com',
 'expedia.com',
 'expediamail.com',
 'priceline.com',
 'costcotravel.com',
 'orbitz.com',
 'travelocity.com',
 'hotwire.com',
 'booking.com',
 'kayak.com')
CODES = {'delta': 'DL',
 'united': 'UA',
 'american': 'AA',
 'southwest': 'WN',
 'jetblue': 'B6',
 'alaska': 'AS',
 'spirit': 'NK',
 'frontier': 'F9',
 'hawaiian': 'HA',
 'allegiant': 'G4',
 'sun country': 'SY',
 'breeze': 'MX',
 'avelo': 'XP',
 'air canada': 'AC',
 'westjet': 'WS',
 'porter': 'PD',
 'british airways': 'BA',
 'virgin atlantic': 'VS',
 'lufthansa': 'LH',
 'swiss': 'LX',
 'austrian': 'OS',
 'air france': 'AF',
 'klm': 'KL',
 'iberia': 'IB',
 'aer lingus': 'EI',
 'tap': 'TP',
 'icelandair': 'FI',
 'norse': 'N0',
 'ryanair': 'FR',
 'easyjet': 'U2',
 'vueling': 'VY',
 'aeromexico': 'AM',
 'volaris': 'Y4',
 'viva': 'VB',
 'copa': 'CM',
 'avianca': 'AV',
 'latam': 'LA',
 'emirates': 'EK',
 'qatar': 'QR',
 'etihad': 'EY',
 'turkish': 'TK',
 'ana': 'NH',
 'japan airlines': 'JL',
 'korean air': 'KE',
 'singapore': 'SQ',
 'cathay': 'CX',
 'qantas': 'QF',
 'air new zealand': 'NZ'}
SHORT = {'DL': 'Delta',
 'AA': 'American',
 'UA': 'United',
 'WN': 'Southwest',
 'B6': 'JetBlue',
 'AS': 'Alaska',
 'NK': 'Spirit',
 'F9': 'Frontier',
 'HA': 'Hawaiian',
 'G4': 'Allegiant',
 'SY': 'Sun Country'}
STATES = frozenset({'alabama',
           'alaska',
           'arizona',
           'arkansas',
           'california',
           'colorado',
           'connecticut',
           'delaware',
           'district of columbia',
           'florida',
           'georgia',
           'hawaii',
           'idaho',
           'illinois',
           'indiana',
           'iowa',
           'kansas',
           'kentucky',
           'louisiana',
           'maine',
           'maryland',
           'massachusetts',
           'michigan',
           'minnesota',
           'mississippi',
           'missouri',
           'montana',
           'nebraska',
           'nevada',
           'new hampshire',
           'new jersey',
           'new mexico',
           'north carolina',
           'north dakota',
           'ohio',
           'oklahoma',
           'oregon',
           'pennsylvania',
           'puerto rico',
           'rhode island',
           'south carolina',
           'south dakota',
           'tennessee',
           'texas',
           'utah',
           'vermont',
           'virginia',
           'west virginia',
           'wisconsin',
           'wyoming'})
NAMES = {'DL': 'Delta Air Lines',
 'UA': 'United Airlines',
 'AA': 'American Airlines',
 'WN': 'Southwest Airlines',
 'B6': 'JetBlue',
 'AS': 'Alaska Airlines',
 'NK': 'Spirit Airlines',
 'F9': 'Frontier Airlines',
 'HA': 'Hawaiian Airlines',
 'G4': 'Allegiant Air',
 'SY': 'Sun Country Airlines',
 'MX': 'Breeze Airways',
 'XP': 'Avelo Airlines',
 'AC': 'Air Canada',
 'WS': 'WestJet',
 'PD': 'Porter Airlines',
 'BA': 'British Airways',
 'VS': 'Virgin Atlantic',
 'LH': 'Lufthansa',
 'LX': 'Swiss International Air Lines',
 'OS': 'Austrian Airlines',
 'AF': 'Air France',
 'KL': 'KLM Royal Dutch Airlines',
 'IB': 'Iberia',
 'EI': 'Aer Lingus',
 'TP': 'TAP Air Portugal',
 'FI': 'Icelandair',
 'N0': 'Norse Atlantic Airways',
 'FR': 'Ryanair',
 'U2': 'easyJet',
 'VY': 'Vueling',
 'AM': 'Aeroméxico',
 'Y4': 'Volaris',
 'VB': 'Viva Aerobus',
 'CM': 'Copa Airlines',
 'AV': 'Avianca',
 'LA': 'LATAM Airlines',
 'EK': 'Emirates',
 'QR': 'Qatar Airways',
 'EY': 'Etihad Airways',
 'TK': 'Turkish Airlines',
 'NH': 'All Nippon Airways',
 'JL': 'Japan Airlines',
 'KE': 'Korean Air',
 'SQ': 'Singapore Airlines',
 'CX': 'Cathay Pacific',
 'QF': 'Qantas',
 'NZ': 'Air New Zealand'}

ICAO = {'DL': 'DAL',
 'UA': 'UAL',
 'AA': 'AAL',
 'WN': 'SWA',
 'B6': 'JBU',
 'AS': 'ASA',
 'NK': 'NKS',
 'F9': 'FFT',
 'HA': 'HAL',
 'G4': 'AAY',
 'SY': 'SCX',
 'MX': 'MXY',
 'XP': 'VXP',
 'AC': 'ACA',
 'WS': 'WJA',
 'PD': 'POE',
 'BA': 'BAW',
 'VS': 'VIR',
 'LH': 'DLH',
 'LX': 'SWR',
 'OS': 'AUA',
 'AF': 'AFR',
 'KL': 'KLM',
 'IB': 'IBE',
 'EI': 'EIN',
 'TP': 'TAP',
 'FI': 'ICE',
 'N0': 'NBT',
 'FR': 'RYR',
 'U2': 'EZY',
 'VY': 'VLG',
 'AM': 'AMX',
 'Y4': 'VOI',
 'VB': 'VIV',
 'CM': 'CMP',
 'AV': 'AVA',
 'LA': 'LAN',
 'EK': 'UAE',
 'QR': 'QTR',
 'EY': 'ETD',
 'TK': 'THY',
 'NH': 'ANA',
 'JL': 'JAL',
 'KE': 'KAL',
 'SQ': 'SIA',
 'CX': 'CPA',
 'QF': 'QFA',
 'NZ': 'ANZ'}


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIPBOX_CONFIG_DIR", str(tmp_path))
    return tmp_path


def test_the_shipped_lists_hold_what_the_code_held(config):
    assert lists.trusted_sender_domains() == DOMAINS
    table = lists.airlines()
    assert {name: a.iata for a in table.values() for name in a.match} == CODES
    assert [w for w, _ in lists.airline_words()] == sorted(CODES, key=lambda w: -len(w))
    assert {a.iata: a.short for a in table.values() if a.short} == SHORT
    assert {a.iata: a.name for a in table.values()} == NAMES
    assert {a.iata: a.icao for a in table.values()} == ICAO
    assert all(len(a.match) == 1 for a in table.values())
    assert lists.us_states() == STATES
    assert lists.overrides_in_use() == ()
    # the files are carried by the package, not only found in the source tree
    shipped = resources.files("tripbox.lists")
    for name in ("senders.yaml", "airlines.yaml", "us_states.txt"):
        assert shipped.joinpath(name).is_file()


def test_a_config_airlines_file_adds_and_replaces(config):
    shipped = dict(lists.airlines())
    (config / "airlines.yaml").write_text(
        "- iata: qx\n  name: Example Air\n  icao: exa\n  match: [Example Air]\n"
        "- iata: DL\n  name: Delta\n"
    )
    lists.clear_caches()
    table = lists.airlines()
    assert carrier_from_name("Example Air Flight") == "QX"
    assert table["QX"].name == "Example Air" and table["QX"].match == ("example air",)
    assert table["DL"].name == "Delta" and table["DL"].short == "Delta"
    assert table["QX"].icao == "EXA" and table["DL"].icao == "DAL"  # a replacement keeps it
    assert carrier_from_name("Delta Air Lines") == "DL"  # the shipped words stay
    assert {k: v for k, v in table.items() if k not in ("QX", "DL")} == {
        k: v for k, v in shipped.items() if k != "DL"
    }
    assert lists.overrides_in_use() == ("airlines.yaml",)


def test_a_config_states_file_adds_names(config):
    (config / "us_states.txt").write_text("# mine\n\nGuam\n  Northern Mariana Islands \n")
    lists.clear_caches()
    states = lists.us_states()
    assert {"guam", "northern mariana islands", "texas"} <= states
    assert len(states) == len(STATES) + 2
    assert lists.overrides_in_use() == ("us_states.txt",)


@pytest.mark.parametrize(
    "text",
    [
        "name: x\n",
        "- iata: Q\n  name: x\n",
        "- iata: QX\n",
        "- iata: QX\n  name: x\n  colour: red\n",
        "- iata: QX\n  name: x\n  match: oops\n",
        "- iata: QX\n  name: x\n  icao: oops\n",
        "- iata: QX\n  name: x\n  icao: QX\n",
        "- iata: QX\n  name: x\n  icao: [EXA]\n",
        "- just text\n",
        "- [unclosed\n",
    ],
)
def test_a_malformed_airlines_file_raises_fixed_text(config, text):
    (config / "airlines.yaml").write_text(text)
    with pytest.raises(lists.ListFileError) as bad:
        lists.airlines()
    assert str(bad.value).startswith("airlines.yaml: ")
    assert "oops" not in str(bad.value) and "colour" not in str(bad.value)


def test_an_unusable_override_file_raises(config):
    (config / "us_states.txt").write_bytes(b"\xff\xfe")
    with pytest.raises(lists.ListFileError):
        lists.us_states()
    lists.clear_caches()
    (config / "us_states.txt").unlink()
    (config / "us_states.txt").symlink_to(config / "nowhere")
    with pytest.raises(lists.ListFileError):
        lists.us_states()
    lists.clear_caches()
    (config / "us_states.txt").unlink()
    (config / "airlines.yaml").write_bytes(b"#" * (64 * 1024 + 1))
    with pytest.raises(lists.ListFileError):
        lists.airlines()


UNREADABLE = "cannot be read (not a regular file of at most 64 KiB, or not readable)"
LOADERS = {lists.AIRLINES_FILE: lists.airlines, lists.STATES_FILE: lists.us_states}


class Blocked(Exception):
    """Raised by the alarm when a load blocks; not an OSError, so no loader takes it for one."""


@pytest.fixture
def never_blocks():
    """Fails the test, instead of hanging it, if a load waits on a pipe."""

    def ring(signum, frame):
        raise Blocked

    old = signal.signal(signal.SIGALRM, ring)
    signal.setitimer(signal.ITIMER_REAL, 5)
    yield
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, old)


@pytest.mark.parametrize("make", ["pipe", "directory"])
@pytest.mark.parametrize("name", sorted(LOADERS))
def test_a_pipe_or_a_directory_as_an_override_is_the_loaders_fixed_problem(
    config, never_blocks, name, make
):
    path = config / name
    if make == "pipe":
        os.mkfifo(path)  # no writer: a blocking open would wait forever
    else:
        path.mkdir()
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(lists.ListFileError) as bad:
        LOADERS[name]()
    assert len(os.listdir("/proc/self/fd")) == before
    assert str(bad.value) == f"{name}: {UNREADABLE}"


def test_an_airlines_file_nested_too_deep_is_not_valid_yaml(config):
    (config / lists.AIRLINES_FILE).write_text("- " + "[" * 3_000 + "]" * 3_000 + "\n")
    with pytest.raises(lists.ListFileError) as bad:
        lists.airlines()
    assert str(bad.value) == "airlines.yaml: not valid YAML"


@pytest.mark.parametrize("value", ["NO", "ON", "no", "on", "10"])
def test_an_iata_that_yaml_does_not_read_as_text_says_to_quote_it(config, value):
    (config / lists.AIRLINES_FILE).write_text(
        f"- iata: QX\n  name: Example Air\n- iata: {value}\n  name: Example Two\n"
    )
    with pytest.raises(lists.ListFileError) as bad:
        lists.airlines()
    assert str(bad.value) == (
        "airlines.yaml: entry 2: iata must be text in quotes"
        " (YAML reads NO and ON as true or false)"
    )
    lists.clear_caches()
    (config / lists.AIRLINES_FILE).write_text(f'- iata: "{value[:2]}"\n  name: Example Two\n')
    assert lists.airlines()[value[:2].upper()].name == "Example Two"


@pytest.mark.parametrize("value", ["YES", "OFF", "yes", "off", "123"])
def test_an_icao_that_yaml_does_not_read_as_text_says_to_quote_it(config, value):
    (config / lists.AIRLINES_FILE).write_text(f"- iata: QX\n  name: Example Air\n  icao: {value}\n")
    with pytest.raises(lists.ListFileError) as bad:
        lists.airlines()
    assert str(bad.value) == (
        "airlines.yaml: entry QX: icao must be text in quotes"
        " (YAML reads YES and OFF as true or false)"
    )
    lists.clear_caches()
    if value.isalpha():
        (config / lists.AIRLINES_FILE).write_text(
            f'- iata: QX\n  name: Example Air\n  icao: "{value}"\n'
        )
        assert lists.airlines()["QX"].icao == value.upper()
