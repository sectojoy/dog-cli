import re

with open('dog/patterns.py', 'r') as f:
    content = f.read()

# We only want to replace delay for network/API errors
# Basically any "delay": <float>, that is >= 1.0 in the RETRY_RULES section.
# We can just look for "delay": 1.0,, "delay": 1.5,, "delay": 2.0,, "delay": 5.0,
# and replace them with "delay": 30.0,
# BUT we should be careful not to match the delays in PERMISSION_RULES. (They are 0.3)

content = re.sub(r'"delay": [125]\.[0-9],', '"delay": 30.0,', content)

with open('dog/patterns.py', 'w') as f:
    f.write(content)
print("Updated patterns.py")
