from pathlib import Path

from fastapi.testclient import TestClient

from hvac_quote.app import create_app


def _login(client: TestClient, username: str = "admin") -> None:
    response = client.post(
        "/login",
        data={"username": username, "password": "password123"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_customer_project_link_contact_and_isolation(tmp_path):
    app = create_app(runs_dir=tmp_path / "runs", data_dir=tmp_path / "data")
    client = TestClient(app)
    client.post(
        "/setup",
        data={
            "display_name": "老板",
            "username": "boss",
            "password": "password123",
            "password_confirm": "password123",
        },
    )
    customer_response = client.post(
        "/customers",
        data={
            "name": "广州测试客户有限公司",
            "short_name": "测试客户",
            "tax_id": "91440000TEST",
            "address": "广州市测试路 1 号",
            "note": "长期客户",
        },
        follow_redirects=False,
    )
    assert customer_response.status_code == 303
    customer_id = customer_response.headers["location"].rsplit("/", 1)[-1]

    customer_page = client.get(f"/customers/{customer_id}")
    assert customer_page.status_code == 200
    assert "广州测试客户有限公司" in customer_page.text
    assert "还没有联系人" in customer_page.text

    new_project = client.get("/projects/new")
    assert new_project.status_code == 200
    assert "广州测试客户有限公司" in new_project.text

    project_response = client.post(
        "/projects",
        data={
            "name": "测试客户一号工程",
            "customer_id": customer_id,
            "customer": "不应覆盖客户档案名称",
            "note": "一号工地",
        },
        follow_redirects=False,
    )
    assert project_response.status_code == 303
    project_id = project_response.headers["location"].rsplit("/", 1)[-1]
    project = app.state.projects.get("boss", project_id)
    assert project is not None
    assert project.customer_id == customer_id
    assert project.customer == "广州测试客户有限公司"

    customer_page = client.get(f"/customers/{customer_id}")
    assert "测试客户一号工程" in customer_page.text

    contact = client.post(
        f"/customers/{customer_id}/contacts",
        data={
            "name": "示例联系人",
            "title": "项目经理",
            "phone": "000-0000-0000",
            "wechat": "chen-test",
            "note": "现场联系人",
            "is_primary": "on",
        },
        follow_redirects=True,
    )
    assert contact.status_code == 200
    assert "示例联系人" in contact.text
    assert "主要" in contact.text
    stored = app.state.customers.get("boss", customer_id)
    assert stored is not None
    assert stored["contacts"][0]["name"] == "示例联系人"
    assert stored["contacts"][0]["isPrimary"] is True

    contract_response = client.post(
        f"/projects/{project_id}/contracts",
        data={
            "title": "测试安装合同",
            "contract_no": "HT-2026-001",
            "counterparty": "广州测试客户有限公司",
            "signing_entity": "测试工程公司",
            "amount": "120000.50",
            "settlement_amount": "",
            "status": "draft",
            "signed_date": "2026-10-19",
            "payment_terms": "预付 30%，验收 70%",
            "note": "测试合同",
        },
        follow_redirects=False,
    )
    assert contract_response.status_code == 303
    contract_id = contract_response.headers["location"].rsplit("/", 1)[-1]
    contract_page = client.get(
        f"/projects/{project_id}/contracts/{contract_id}"
    )
    assert contract_page.status_code == 200
    assert "测试安装合同" in contract_page.text
    assert "120000.50" in contract_page.text
    listed = client.get(f"/projects/{project_id}/contracts")
    assert "HT-2026-001" in listed.text

    updated = client.post(
        f"/projects/{project_id}/contracts/{contract_id}",
        data={
            "title": "测试安装合同",
            "contract_no": "HT-2026-001",
            "counterparty": "广州测试客户有限公司",
            "signing_entity": "测试工程公司",
            "amount": "120000.50",
            "settlement_amount": "118000",
            "status": "signed",
            "signed_date": "2026-10-19",
            "payment_terms": "预付 30%，验收 70%",
            "note": "已签",
        },
        follow_redirects=True,
    )
    assert updated.status_code == 200
    assert "已签署" in updated.text
    stored_contract = app.state.contracts.get(
        "boss", project_id, contract_id
    )
    assert stored_contract is not None
    assert stored_contract["status"] == "signed"
    assert stored_contract["settlementAmount"] == "118000"

    app.state.auth.create_user("other", "password123", "其他用户")
    other = TestClient(app)
    _login(other, "other")
    forbidden_customer = other.get(f"/customers/{customer_id}")
    assert forbidden_customer.status_code == 404
    forbidden_project = other.get(f"/projects/{project_id}")
    assert forbidden_project.status_code == 404
    forbidden_contract = other.get(
        f"/projects/{project_id}/contracts/{contract_id}"
    )
    assert forbidden_contract.status_code == 404
