import unittest
from fastapi.testclient import TestClient
from app.main import app, job_manager, DownloadRequest


class TestAPI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        job_manager.queue.clear()
        job_manager.active_job = None

    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")

    def test_config(self):
        response = self.client.get("/api/config")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("port", data)
        self.assertIn("default_language", data)

    def test_status_idle(self):
        job_manager.active_job = None
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "idle")

    def test_download_validation(self):
        # Missing URL
        response = self.client.post("/api/download", json={})
        self.assertEqual(response.status_code, 422)

        # Invalid format
        response = self.client.post("/api/download", json={
            "url": "https://www.youtube.com/@example",
            "format": "mp4",
        })
        self.assertEqual(response.status_code, 422)

    def test_enqueue_and_queue_api(self):
        # Enqueue item 1
        res1 = self.client.post("/api/download", json={
            "url": "https://www.youtube.com/@channel1",
            "format": "txt",
        })
        self.assertEqual(res1.status_code, 200)
        data1 = res1.json()
        self.assertIn("job", data1)
        job1_id = data1["job"]["job_id"]

        # Enqueue item 2
        res2 = self.client.post("/api/download", json={
            "url": "https://www.youtube.com/@channel2",
            "format": "srt",
        })
        self.assertEqual(res2.status_code, 200)
        job2_id = res2.json()["job"]["job_id"]

        # Check queue
        q_res = self.client.get("/api/queue")
        self.assertEqual(q_res.status_code, 200)
        q_data = q_res.json()
        self.assertIn("queue", q_data)

        # Reorder queue
        reorder_res = self.client.post("/api/queue/reorder", json={
            "job_ids": [job2_id, job1_id]
        })
        self.assertEqual(reorder_res.status_code, 200)

        # Move item
        move_res = self.client.post(f"/api/queue/{job1_id}/move", json={"direction": "up"})
        self.assertEqual(move_res.status_code, 200)

        # Delete from queue
        del_res = self.client.delete(f"/api/queue/{job2_id}")
        self.assertEqual(del_res.status_code, 200)
        self.assertTrue(del_res.json()["success"])

    def test_skip_active_endpoint(self):
        skip_res = self.client.post("/api/queue/skip-active", json={"requeue": True})
        self.assertEqual(skip_res.status_code, 200)
        self.assertIn("skipped", skip_res.json())


if __name__ == "__main__":
    unittest.main()
