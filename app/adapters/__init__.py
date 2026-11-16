from .aqara import AqaraAdapter
from .demo import DemoAdapter
from .ecowitt import EcowittAdapter
from .hue import HueAdapter
from .mqtt import MqttAdapter
from .nut import NutAdapter
from .ring import RingAdapter
from .xsense import XSenseAdapter
from .yolink import YoLinkAdapter

ADAPTERS = {
    "yolink": YoLinkAdapter,
    "aqara": AqaraAdapter,
    "ecowitt": EcowittAdapter,
    "xsense": XSenseAdapter,
    "mqtt": MqttAdapter,
    "nut": NutAdapter,
    "hue": HueAdapter,
    "ring": RingAdapter,
    "demo": DemoAdapter,
}
