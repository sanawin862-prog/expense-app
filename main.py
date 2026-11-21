with open("updated_main.py", "r", encoding="utf-8") as f:
    src = f.read()

bad_line = "f'onsubmit=\"return confirm('ลบรายการนี้นะ?')\">'"
good_line = "f'onsubmit=\"return confirm(&#39;ลบรายการนี้นะ?&#39;)\">'"

if bad_line in src:
    src = src.replace(bad_line, good_line)
else:
    # fallback replace if quotes differ slightly
    import re
    src = re.sub(r"onsubmit=\"return confirm\('ลบรายการนี้นะ\?'\)\"", 'onsubmit="return confirm(&#39;ลบรายการนี้นะ?&#39;)"', src)

with open("fixed_main.py", "w", encoding="utf-8") as f:
    f.write(src)

# Verify syntax with py_compile
import py_compile
py_compile.compile("fixed_main.py", doraise=True)
print("fixed_main.py compiled successfully!")