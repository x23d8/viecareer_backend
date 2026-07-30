from unittest.mock import MagicMock, patch

from services.file_service import save_uploaded_file


def test_save_uploaded_file_is_scoped_and_deduplicated():
    cursor = MagicMock()
    cursor.fetchone.side_effect = [
        {"id": "internal-user-id"},
        {
            "id": "file-id",
            "file_name": "resume.pdf",
            "file_type": "pdf",
            "mime_type": "application/pdf",
            "file_size_bytes": 7,
            "storage_url": "database://uploaded-files/extracted-text",
            "extracted_text": "CV text",
            "file_purpose": "cv",
        },
    ]
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    connection.__enter__.return_value = connection

    with patch("services.file_service.get_connection", return_value=connection):
        payload = save_uploaded_file(
            external_auth_id="clerk-user",
            filename="resume.pdf",
            mime_type="application/pdf",
            file_bytes=b"CV text",
            extracted_text="CV text",
            purpose="cv",
        )

    assert payload["id"] == "file-id"
    assert payload["file_purpose"] == "cv"
    insert_call = next(
        call for call in cursor.execute.call_args_list if "insert into uploaded_files" in call.args[0]
    )
    assert "on conflict (user_id, file_purpose, content_sha256)" in insert_call.args[0]
    assert "where content_sha256 is not null" in insert_call.args[0]
    connection.commit.assert_called_once()
