from fastapi.testclient import TestClient
from hvac_quote.app import create_app


def test_private_project_endpoints_are_not_published(tmp_path):
    with TestClient(create_app(tmp_path / 'runs', data_dir=tmp_path / 'data')) as client:
        client.post('/setup', data={'display_name':'Demo User','username':'demo','password':'synthetic-test-password','password_confirm':'synthetic-test-password'})
        for endpoint in ['/admin-first', '/run', '/teaching-2-3']:
            response = client.get(endpoint) if endpoint != '/run' else client.post(endpoint, data={})
            assert response.status_code == 404, endpoint
        assert client.get('/projects').status_code == 200
        assert client.get('/health').status_code == 200


def test_orphan_runtime_files_cannot_be_downloaded(tmp_path):
    from hvac_quote.app import _new_run_id
    run_id = _new_run_id()
    run_dir = tmp_path / 'runs' / run_id
    run_dir.mkdir(parents=True)
    (run_dir / 'quote.json').write_text('{"synthetic": true}')
    with TestClient(create_app(tmp_path / 'runs', data_dir=tmp_path / 'data')) as client:
        client.post('/setup', data={'display_name':'Demo User','username':'demo','password':'synthetic-test-password','password_confirm':'synthetic-test-password'})
        assert client.get(f'/runs/{run_id}/quote.json').status_code == 404
