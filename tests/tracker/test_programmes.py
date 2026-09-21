import pytest

from app.scouting.programmes import classify_programme


@pytest.mark.parametrize('title, expected', [
    ('Deal Advisory - Placement Student 2027', 'year_in_industry'),
    ('Accountancy - Placement Student 2027', 'year_in_industry'),
    ('Summer Placement 2027', 'summer'),
    ('Industrial Placement - Summer 2027 start', 'year_in_industry'),
    ('Placement Consultant', 'other'),
    ('Summer Internship 2027', 'summer'),
])
def test_tracker_reference_programmes(title, expected):
    assert classify_programme(title).value == expected
