"""最近网格点采用球面距离并保留原二维索引。"""

import unittest

import numpy as np

from wrf_evaluate.spatial import SpatialMatchError, nearest_grid_point, nearest_rectilinear_point


class NearestGridPointTests(unittest.TestCase):
    def test_finds_nearest_by_geographic_distance_and_returns_row_column(self):
        longitude = np.array([[87.0, 87.5], [88.0, 88.5]])
        latitude = np.array([[43.0, 43.0], [43.5, 43.5]])
        result = nearest_grid_point(87.51, 43.01, longitude, latitude)
        self.assertEqual((result.row, result.column), (0, 1))
        self.assertGreater(result.distance_km, 0)
        self.assertTrue(result.inside_domain)

    def test_wraps_longitude_and_does_not_include_antipodal_point(self):
        result = nearest_grid_point(
            -179.9, 0, [[179.8, -179.8], [179.8, -179.8]], [[-0.1, -0.1], [0.1, 0.1]],
        )
        self.assertEqual(result.column, 1)
        self.assertLess(result.distance_km, 40)
        self.assertTrue(result.inside_domain)
        antipodal = nearest_grid_point(0, 0, [[179.8, -179.8], [179.8, -179.8]], [[-1, -1], [1, 1]])
        self.assertFalse(antipodal.inside_domain)

    def test_outside_polygon_not_just_bounding_box_and_boundary_is_included(self):
        longitude = [[0, 1], [1, 2]]
        latitude = [[0, 0], [1, 1]]
        self.assertFalse(nearest_grid_point(0.1, 0.9, longitude, latitude).inside_domain)
        edge = nearest_grid_point(1, 0, longitude, latitude)
        self.assertTrue(edge.inside_domain)
        self.assertEqual(edge.distance_km, 0)

    def test_rejects_bad_coordinates_and_grid_shapes(self):
        for args in [
            (181, 0, [[0]], [[0]]),
            (0, float("nan"), [[0]], [[0]]),
            (0, 0, [[0]], [[0, 1]]),
            (0, 0, np.array([[np.nan]]), np.array([[np.nan]])),
        ]:
            with self.subTest(args=args), self.assertRaises(SpatialMatchError):
                nearest_grid_point(*args)


class RectilinearPointTests(unittest.TestCase):
    def test_axis_search_matches_full_spherical_grid_for_ascending_and_descending_axes(self):
        for longitudes in (np.linspace(60, 159, 24), np.linspace(159, 60, 24)):
            for latitudes in (np.linspace(1, 80, 17), np.linspace(80, 1, 17)):
                lon_grid, lat_grid = np.meshgrid(longitudes, latitudes)
                for lon, lat in ((87.4897, 43.4733), (60, 1), (159, 80), (139.1, 72.6), (50, -10)):
                    with self.subTest(lon=lon, lat=lat):
                        expected = nearest_grid_point(lon, lat, lon_grid, lat_grid)
                        actual = nearest_rectilinear_point(lon, lat, longitudes, latitudes)
                        self.assertEqual((actual.row, actual.column), (expected.row, expected.column))
                        self.assertEqual(actual.inside_domain, expected.inside_domain)
                        self.assertAlmostEqual(actual.distance_km, expected.distance_km, places=8)

    def test_invalid_axes_and_point_are_rejected(self):
        for lons, lats in (
            ([87], [43, 44]), ([87, 87], [43, 44]), ([87, np.nan], [43, 44]),
            ([87, 89, 88], [43, 44]), ([87, 88], [90, 91]),
            ([87, 88], np.ma.array([43, 44], mask=[False, True])),
            ([-179, 179], [43, 44]),
        ):
            with self.subTest(lons=lons, lats=lats), self.assertRaises(SpatialMatchError):
                nearest_rectilinear_point(87.5, 43.5, lons, lats)
        with self.assertRaises(SpatialMatchError):
            nearest_rectilinear_point(True, 43, [87, 88], [43, 44])


if __name__ == "__main__":
    unittest.main()
