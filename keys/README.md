# Age Keys

- Store the **public** Age key that SOPS should use in `age-public.txt`.
- Never commit the private key. Keep it in the platform password manager and offline backups only.
- When rotating keys:
  1. Generate new key pair (`age-keygen -o age-private.txt`).
  2. Copy the public block into `age-public.txt`.
  3. Update `.sops.yaml` with the new recipient string.
  4. Run `sops updatekeys -y <encrypted file>` for every encrypted secret.
  5. Securely delete `age-private.txt` after storing it in approved vaults.
