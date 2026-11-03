from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra):
        if not email:
            raise ValueError("email is required")
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.update(is_staff=True, is_superuser=True, approved=True, email_verified=True)
        return self._create_user(email, password, **extra)


class User(AbstractUser):
    """Login identifier is the e-mail address. No real names are stored."""

    username = None
    first_name = None
    last_name = None
    email = models.EmailField("E-Mail", unique=True)
    nickname = models.CharField("Nickname", max_length=64)
    email_verified = models.BooleanField("E-Mail bestätigt", default=False)
    approved = models.BooleanField("Freigeschaltet", default=False)

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = ["nickname"]

    objects = UserManager()

    class Meta:
        verbose_name = "Benutzer*in"
        verbose_name_plural = "Benutzer*innen"

    def __str__(self):
        return self.nickname or self.email

    @property
    def can_login(self):
        return self.is_active and self.email_verified and self.approved
