"""The ship file sets the bounded refresh interval for unchanged status."""

import pytest

from quarterdeck import ship as shipmod


def load(tmp_path, value=''):
    (tmp_path / 'ship.toml').write_text('[ship]\n' + value)
    return shipmod.load(tmp_path)


def test_the_default_refresh_is_thirty_seconds(tmp_path):
    assert load(tmp_path).status_refresh == shipmod.STATUS_REFRESH == 30.0


@pytest.mark.parametrize('value', [5, 5.25, 30, 45.5, 300])
def test_the_ship_keeps_a_refresh_within_the_inclusive_bounds(tmp_path, value):
    ship = load(tmp_path, f'status_refresh={value}\n')
    assert ship.status_refresh == value
    assert type(ship.status_refresh) is float


@pytest.mark.parametrize('written', [
    '4.9999', '300.0001', '0', '-1', 'nan', 'inf', '-inf', '1e400',
    'true', 'false', '"30"', '[30]', '{ seconds=30 }', '1' + '0' * 400,
])
def test_invalid_refresh_is_refused_with_the_setting_and_range(tmp_path, written):
    with pytest.raises(shipmod.ShipError, match=r'ship\.status_refresh must be a number of seconds from 5 to 300; got '):
        load(tmp_path, f'status_refresh={written}\n')
