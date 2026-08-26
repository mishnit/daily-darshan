"""Small official temple adapters used by the weekday rotation registry."""
from .darshan_page import DarshanPageSource

class _TemplePageSource(DarshanPageSource):
    keywords = []
    page_is_dated = False
    date_parameter = False
    def __init__(self, page_url, **kwargs):
        super().__init__(page_url, self.keywords, check_page_date=self.page_is_dated,
                         date_parameter=self.date_parameter, **kwargs)

class MahakalSource(_TemplePageSource):
    name, keywords, page_is_dated = "mahakal", ["mahakal", "bhasma", "darshan", "shiva"], True
class SalangpurSource(_TemplePageSource):
    name, keywords, date_parameter = "salangpur", ["salangpur", "kashtbhanjan", "hanuman", "darshan"], True
class IskconBangaloreSource(_TemplePageSource):
    name, keywords, page_is_dated = "iskcon_bangalore", ["iskcon", "bangalore", "krishna", "radha", "darshan"], True
class IskconVrindavanSource(_TemplePageSource):
    name, keywords = "iskcon_vrindavan", ["vrindavan", "krishna", "radha", "darshan"]
class IskconTirupatiSource(_TemplePageSource):
    name, keywords = "iskcon_tirupati", ["tirupati", "krishna", "darshan"]
class SwaminarayanSource(_TemplePageSource):
    name, keywords = "swaminarayan", ["swaminarayan", "darshan", "vishnu"]
class MayapurSource(_TemplePageSource):
    name, keywords = "mayapur", ["mayapur", "krishna", "radha", "darshan"]
