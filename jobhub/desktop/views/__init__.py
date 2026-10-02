"""Page views for the Starployment desktop window. Each module owns one sidebar destination."""
from .applications import ApplicationsView
from .dashboard import DashboardView
from .jobs import JobsView
from .profile import ProfileView
from .settings import SettingsView
from .tracker import TrackerView

__all__ = ["ApplicationsView", "DashboardView", "JobsView", "ProfileView", "SettingsView", "TrackerView"]
