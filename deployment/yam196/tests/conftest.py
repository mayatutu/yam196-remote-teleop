from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent.parent
RELEASE = HERE if (HERE / 'python').is_dir() else HERE / '.build/release'
sys.path[:0] = [str(RELEASE / 'python'), str(RELEASE / 'arena-client/yam_client'),
                str(RELEASE / 'piper-mate/Python_SDK')]
# Exercise editable adapters, while using frozen upstream modules/calibration.
import yam_deploy
yam_deploy.__path__.insert(0, str(HERE / 'yam_deploy'))
