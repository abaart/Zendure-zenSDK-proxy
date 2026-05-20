from __future__ import annotations


TWO_DEVICE_AGGREGATES = {
    "BatVolt": 5250,
    "remainOutTime": 150,
    "hyperTmp": 2761,
    "socStatus": 0,
    "gridReverse": 1,
    "pass": -1,
    "batCalTime": -1,
    "pvStatus": 2,
    "acStatus": 1,
    "dcStatus": 3,
}


THREE_DEVICE_ELECTRIC_LEVEL_WITH_TWO_EMPTY = 11


DISTRIBUTION_AFTER_MAX_CLIPPING = {
    "avail": [50, 40, 10],
    "total": 1600,
    "max_power": 800,
    "balancing_factor": 5,
    "expected": [800, 799, 0],
}


STANDBY_POST_PROPERTIES = {
    "smartMode": 0,
    "outputLimit": 0,
    "inputLimit": 0,
}
