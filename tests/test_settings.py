import json

import pytest

from app.settings import ROOT, Settings


def base_manifest():
    return json.loads((ROOT/"config/pages.test.json").read_text(encoding="utf-8"))


def settings_for(tmp_path,manifest,production=False,**overrides):
    path=tmp_path/"pages.json"
    path.write_text(json.dumps(manifest,ensure_ascii=False),encoding="utf-8")
    values={"page_config":path,"session_secret":"unit-test-session-"+"s"*40,
            "researcher_password_hash":"unit-test-hash"}
    if production:
        values.update(app_env="production",backend_mode="supabase",cookie_secure=True,
                      participant_token_secret="unit-test-participant-"+"p"*40,
                      app_base_url="https://study.example.org",allowed_hosts=["study.example.org"],
                      database_url="postgresql://unused:unused@example.invalid/study?sslmode=require",
                      supabase_url="https://example.supabase.co",supabase_key="sb_secret_unit_test")
    values.update(overrides)
    return Settings(**values)


def production_manifest():
    manifest=base_manifest()
    manifest.update(profile="production",consent_version="approved-2026-v1",
                    consent_text="研究目的、利用範囲、保管期限、撤回方法を記載した確定同意文。")
    manifest["geometry"]["calibrated"]=True
    manifest["worksheet"]["test_only"]=False
    for page in manifest["pages"]:
        page["qr_code"]=f"STUDY-{page['page_number']:02d}"
    return manifest


def test_local_test_profile_and_sorting(tmp_path):
    manifest=base_manifest()
    manifest["pages"].reverse()
    result=settings_for(tmp_path,manifest).load_manifest()
    assert [p["page_number"] for p in result["pages"]]==[1,2,3]
    assert result["profile"]=="test"


def test_complete_production_configuration_loads_without_external_access(tmp_path):
    result=settings_for(tmp_path,production_manifest(),production=True).load_manifest()
    assert result["geometry"]["calibrated"] is True


@pytest.mark.parametrize("field,value",[("calibrated",False),("calibrated","false"),
                                      ("output_width",True),("output_width",99999),
                                      ("min_focus",float("nan")),
                                      ("marker_centers_mm",[[20,20]]*4),
                                      ("marker_centers_mm",[[0,20],[190,20],[190,277],[20,277]]),
                                      ("qr_center_region",[.8,.8,.2,.9]),
                                      ("qr_center_region",[0,0,2,1]),
                                      ("qr_size_mm",[25,2])])
def test_production_rejects_uncalibrated_or_malformed_geometry(tmp_path,field,value):
    manifest=production_manifest()
    manifest["geometry"][field]=value
    with pytest.raises(ValueError):
        settings_for(tmp_path,manifest,production=True).load_manifest()


@pytest.mark.parametrize("field,value",[("profile","test"),("consent_version","test-v1"),
                                      ("consent_text","これは動作確認用の文面です。")])
def test_production_rejects_test_profile_or_consent(tmp_path,field,value):
    manifest=production_manifest()
    manifest[field]=value
    with pytest.raises(ValueError):
        settings_for(tmp_path,manifest,production=True).load_manifest()


@pytest.mark.parametrize("overrides",[{"cookie_secure":False},{"app_base_url":"http://study.example.org"},
                                     {"backend_mode":"local"},{"allowed_hosts":["*"]},
                                     {"participant_token_secret":"short"},{"session_secret":"short"},
                                     {"supabase_key":""}])
def test_production_rejects_incomplete_server_settings(tmp_path,overrides):
    with pytest.raises(ValueError):
        settings_for(tmp_path,production_manifest(),production=True,**overrides).load_manifest()


@pytest.mark.parametrize("field,value",[("page_number",True),("page_number",0),
                                      ("required","false"),("qr_code","../escape"),
                                      ("qr_code","=spreadsheet_formula")])
def test_malformed_pages_fail_at_startup(tmp_path,field,value):
    manifest=base_manifest()
    manifest["pages"][0][field]=value
    with pytest.raises(ValueError):
        settings_for(tmp_path,manifest).load_manifest()


@pytest.mark.parametrize("field",["page_number","qr_code"])
def test_duplicate_page_mapping_rejected(tmp_path,field):
    manifest=base_manifest()
    manifest["pages"][1][field]=manifest["pages"][0][field]
    with pytest.raises(ValueError):
        settings_for(tmp_path,manifest).load_manifest()


def test_at_least_one_required_page(tmp_path):
    manifest=base_manifest()
    for page in manifest["pages"]:
        page["required"]=False
    with pytest.raises(ValueError):
        settings_for(tmp_path,manifest).load_manifest()
