
Have these installed with os package manager:
```
ccache
patchelf
```

Install this (onfile comes with compression library)
```
pip install Nuitka[onefile]
```


# Find editable file:
```
$ python3 -c "import RNS.Interfaces as i; print(i.__file__)"
/home/user/.local/share/mamba/envs/retiboard/lib/python3.11/site-packages/RNS/Interfaces/__init__.py
```

# Extract line to inject
```
$ python3 -c "import RNS.Interfaces as i; print('__all__ =', i.__all__)"
__all__ = ['UDPInterface', 'AX25KISSInterface', 'SerialInterface', 'I2PInterface', 'KISSInterface', 'Interface', 'WeaveInterface', 'BackboneInterface', 'RNodeInterface', 'AutoInterface', 'TCPInterface', 'PipeInterface', 'RNodeMultiInterface', 'LocalInterface']
```

# Edit file like this
```
import os
import glob
import RNS.Interfaces.Android
import RNS.Interfaces.util
import RNS.Interfaces.util.netinfo as netinfo

#py_modules  = glob.glob(os.path.dirname(__file__)+"/*.py")
#pyc_modules = glob.glob(os.path.dirname(__file__)+"/*.pyc")
#modules     = py_modules+pyc_modules
#__all__ = list(set([os.path.basename(f).replace(".pyc", "").replace(".py", "") for f in modules if not (f.endswith("__init__.py") or f.endswith("__init__.pyc"))]))

__all__ = ['SerialInterface', 'TCPInterface', 'LocalInterface', 'KISSInterface', 'WeaveInterface', 'AutoInterface', 'Interface', 'UDPInterface', 'RNodeMultiInterface', 'PipeInterface', 'RNodeInterface', 'AX25KISSInterface', 'I2PInterface', 'BackboneInterface']
```

# compile binary
```
python3 -m nuitka --standalone \
                  --onefile \
                  --follow-imports \
                  --include-package=RNS \
                  --include-package=LXMF \
                  --include-data-files=retiboard/db/schema.sql=retiboard/db/schema.sql \
                  --include-data-dir=frontend/dist=frontend/dist \
                  --output-filename=retiboard-linux \
                  retiboard/main.py
```
