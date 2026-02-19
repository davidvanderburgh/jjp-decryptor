import os
os.makedirs('C:/tmp', exist_ok=True)

content = """test
content"""
with open('C:/tmp/test3.txt', 'w') as f:
    f.write(content)
print(f'Wrote {len(content)} bytes')
