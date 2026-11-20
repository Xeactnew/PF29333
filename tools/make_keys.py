"""Print fresh secret keys for your .env file."""
import secrets

from cryptography.fernet import Fernet

print(f"PF_SECRET_KEY={secrets.token_urlsafe(48)}")
print(f"PF_FERNET_KEY={Fernet.generate_key().decode()}")
print("\nKeep PF_FERNET_KEY safe and never change it: it unlocks the saved MT5 passwords.")
