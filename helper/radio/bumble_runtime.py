"""One pinned Bumble source resolver for packaged and source-checkout runs."""
from pathlib import Path
import importlib.metadata
import json
import sys

BUMBLE_COMMIT = 'a679beb4f36681c6f1b6d72b7e825c6f57f9ef83'

def ensure_bumble_source():
    root=Path(__file__).resolve().parents[1]
    # Both are explicit project-owned locations, never a random global installation.
    candidates=(root/'runtime/python/Lib/site-packages',root/'references/bumble')
    loaded=sys.modules.get('bumble')
    loaded_dir=Path(loaded.__file__).resolve().parent if loaded is not None else None
    selected=next((p for p in candidates if loaded_dir==(p/'bumble').resolve()),None)
    if selected is None:selected=next((p for p in candidates if (p/'bumble/__init__.py').is_file()),None)
    if selected is None:
        # Source users install the commit pinned in runtime/requirements.txt.
        try:
            dist=importlib.metadata.distribution('bumble')
            source=json.loads(dist.read_text('direct_url.json') or '{}')
            if source.get('vcs_info',{}).get('commit_id')!=BUMBLE_COMMIT:
                raise ImportError('Bumble installation does not match the required commit')
            selected=Path(dist.locate_file(''))
        except (importlib.metadata.PackageNotFoundError,ValueError) as error:
            raise ImportError('Install the pinned Bumble dependency from runtime/requirements.txt') from error
    if loaded_dir is not None and loaded_dir!=(selected/'bumble').resolve():
        raise ImportError('A different Bumble runtime is already loaded')
    location=str(selected)
    if location not in sys.path:sys.path.insert(0,location)
    return selected
