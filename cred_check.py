import os

addr = os.environ.get("GMAIL_ADDRESS", "")
pw = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")

print("Address length:", len(addr), "| plain text:", addr.isascii(),
      "| ends with @gmail.com:", addr.strip().lower().endswith("@gmail.com"))
print("Password length:", len(pw), "| plain text:", pw.isascii(),
      "| letters only:", pw.strip().isalpha())