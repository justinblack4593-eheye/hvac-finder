import os, smtplib

addr = os.environ["GMAIL_ADDRESS"].strip()
pw = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "").strip()

s = smtplib.SMTP("smtp.gmail.com", 587, timeout=30)
s.set_debuglevel(1)
s.ehlo()
s.starttls()
s.ehlo()
s.login(addr, pw)
print("\nLOGIN OK")
s.quit()