"""Page views for the JobHub desktop window. Each module owns one sidebar destination."""
from .applications import ApplicationsView
from .dashboard import DashboardView
from .jobs import JobsView
from .profile import ProfileView
from .settings import SettingsView

__all__ = ["ApplicationsView", "DashboardView", "JobsView", "ProfileView", "SettingsView"]
