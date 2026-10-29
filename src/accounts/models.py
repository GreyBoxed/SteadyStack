from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """Single operator. Custom from the start because swapping later is a
    migration nobody enjoys."""
