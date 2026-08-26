"""Image source implementations (Tech Doc section 10)."""
from .temple_source import TempleSource
from .rss_source import RSSSource
from .website_source import WebsiteSource
from .validator import ImageValidator
from .temples import MahakalSource, SalangpurSource, IskconBangaloreSource, IskconVrindavanSource, IskconTirupatiSource, SwaminarayanSource, MayapurSource

__all__ = ["TempleSource", "RSSSource", "WebsiteSource", "ImageValidator", "MahakalSource", "SalangpurSource", "IskconBangaloreSource", "IskconVrindavanSource", "IskconTirupatiSource", "SwaminarayanSource", "MayapurSource"]
