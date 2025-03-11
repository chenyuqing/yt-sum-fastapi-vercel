from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import requests
import json
import os
from datetime import datetime
import uuid
from pydantic import BaseModel
from typing import List, Optional
import requests as req  # Rename to avoid potential conflicts
from dotenv import load_dotenv
import traceback

# Load environment variables
load_dotenv()

app = FastAPI(title="YouTube Video Summarizer")

# Mount static files directory
app.mount("/static", StaticFiles(directory="static"), name="static")

# Set up templates and custom filters
templates = Jinja2Templates(directory="templates")

# Add custom datetime filter
def datetime_format(value, format='%Y-%m-%d %H:%M'):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return value.strftime(format)

templates.env.filters["datetimeformat"] = datetime_format

# JSON文件存储路径
SUMMARY_FILE = "summaries.json"

# 加载已有总结
try:
    with open(SUMMARY_FILE, 'r') as f:
        summaries = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    summaries = []


# 保存总结到文件
def save_summaries():
    global summaries
    try:
        with open(SUMMARY_FILE, 'w') as f:
            json.dump(summaries, f, ensure_ascii=False, indent=2)
        # 创建备份文件
        backup_file = f"{SUMMARY_FILE}.bak"
        with open(backup_file, 'w') as f:
            json.dump(summaries, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存失败: {str(e)}")
        # 尝试从备份恢复
        if os.path.exists(backup_file):
            with open(backup_file, 'r') as f:
                summaries = json.load(f)

class Summary(BaseModel):
    id: str
    youtube_url: str
    video_id: str
    title: str
    summary: str
    created_at: str

# 分页获取历史记录
@app.get("/api/history")
async def get_history(page: int = 1, per_page: int = 10):
    global summaries
    start = (page - 1) * per_page
    end = start + per_page
    return {
        "summaries": summaries[start:end],
        "total": len(summaries),
        "page": page,
        "per_page": per_page
    }

# 删除历史记录
@app.delete("/api/history/{summary_id}")
async def delete_summary(summary_id: str):
    global summaries
    original_count = len(summaries)
    summaries = [s for s in summaries if s["id"] != summary_id]
    
    if len(summaries) == original_count:
        raise HTTPException(status_code=404, detail="Summary not found")
    
    try:
        save_summaries()
        return {"status": "success", "message": "记录已删除"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除失败: {str(e)}")

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

def extract_video_id(youtube_url):
    """Extract the video ID from a YouTube URL."""
    if "youtu.be" in youtube_url:
        return youtube_url.split("/")[-1].split("?")[0]
    elif "youtube.com" in youtube_url:
        if "v=" in youtube_url:
            return youtube_url.split("v=")[1].split("&")[0]
    return youtube_url  # If it's already just the ID

def get_transcript(video_id, api_key):
    """Get the transcript from SearchAPI.io."""
    # Validate inputs
    if not video_id or not video_id.strip():
        raise HTTPException(status_code=400, detail="Invalid YouTube video ID")
    
    if not api_key or len(api_key.strip()) < 10:
        raise HTTPException(status_code=400, detail="Invalid SearchAPI.io API key. Please provide a valid API key.")
    
    url = "https://www.searchapi.io/api/v1/search"
    params = {
        "engine": "youtube_transcripts",
        "video_id": video_id,
        "api_key": api_key
    }
    
    try:
        response = requests.get(url, params=params, timeout=15)
        
        # Check for HTTP errors
        if response.status_code != 200:
            error_detail = "Unknown error"
            try:
                error_data = response.json()
                if "error" in error_data:
                    error_detail = error_data["error"]
                else:
                    error_detail = str(error_data)
            except:
                error_detail = response.text or f"HTTP Error: {response.status_code}"
            
            raise HTTPException(status_code=400, 
                                detail=f"Failed to fetch transcript: {error_detail}")
        
        # Parse the JSON response
        try:
            data = response.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, 
                                detail="Invalid response from SearchAPI.io. The API did not return valid JSON.")
        
        # Check if transcripts exist in the response
        if "transcripts" not in data:
            if "error" in data:
                raise HTTPException(status_code=400, 
                                    detail=f"SearchAPI.io error: {data['error']}")
            else:
                raise HTTPException(status_code=400, 
                                    detail="No transcript found for this video. The video might not have captions available.")
        
        if not data["transcripts"] or len(data["transcripts"]) == 0:
            raise HTTPException(status_code=400, 
                                detail="The transcript for this video is empty. The video might not have proper captions.")
        
        # Combine all transcript segments into one text
        full_transcript = " ".join([segment["text"] for segment in data["transcripts"]])
        return full_transcript, data
    
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, 
                            detail="SearchAPI.io request timed out. Please try again later.")
    except requests.exceptions.ConnectionError:
        raise HTTPException(status_code=503, 
                            detail="Could not connect to SearchAPI.io. Please check your internet connection.")
    except HTTPException:
        # Re-raise HTTPExceptions directly without wrapping them
        raise
    except Exception as e:
        # Improve error handling with more detailed information
        error_type = type(e).__name__
        error_message = str(e)
        print(f"Transcript fetch error: {error_type} - {error_message}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, 
                            detail=f"An unexpected error occurred while fetching the transcript: {error_message}")


def get_video_title(video_id):
    """Get the video title using YouTube API or scraping."""
    # This is a simplified version - in a real app, you might use YouTube Data API
    try:
        url = f"https://www.youtube.com/watch?v={video_id}"
        response = requests.get(url)
        if response.status_code == 200:
            # Very basic title extraction - not reliable for production
            title_start = response.text.find("<title>") + 7
            title_end = response.text.find("</title>")
            if title_start > 0 and title_end > 0:
                title = response.text[title_start:title_end]
                return title.replace(" - YouTube", "")
    except Exception as e:
        print(f"Error getting video title: {e}")
    
    return f"Video {video_id}"

def summarize_with_deepseek(transcript, deepseek_api_key):
    """Summarize the transcript using DeepSeek API."""
    try:
        # Validate API key
        if not deepseek_api_key or len(deepseek_api_key.strip()) < 10:
            raise ValueError("Invalid DeepSeek API key. Please provide a valid API key.")

        # Initialize OpenAI client with DeepSeek base URL
        from openai import OpenAI
        client = OpenAI(
            api_key=deepseek_api_key,
            base_url="https://api.deepseek.com/v1"
        )

        # Create a prompt for summarization
        prompt = f"""Please summarize the following YouTube video transcript into concise bullet points that capture the main ideas and key information. Focus on the most important concepts, arguments, and takeaways.

Transcript:
{transcript}

Summary (in bullet points):"""

        try:
            # Create chat completion using the OpenAI SDK
            response = client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant that creates concise bullet-point summaries."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=1000
            )

            # Extract the summary from the response
            summary = response.choices[0].message.content
            return summary.replace('\n', '<br>')

        except Exception as api_error:
            print(f"DeepSeek API error: {type(api_error).__name__} - {str(api_error)}")
            print(traceback.format_exc())
            raise HTTPException(status_code=500, detail=f"Error with DeepSeek API: {str(api_error)}")

    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        print(f"Unexpected error: {type(e).__name__} - {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {str(e)}")
    
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="DeepSeek API request timed out. Please try again later.")
    except requests.exceptions.ConnectionError:
        raise HTTPException(status_code=503, detail="Could not connect to DeepSeek API. Please check your internet connection.")
    except Exception as e:
        print(f"DeepSeek API error: {type(e).__name__} - {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Error with DeepSeek API: {str(e)}")

@app.post("/summarize")
async def summarize(request: Request, youtube_url: str = Form(...), deepseek_api_key: str = Form(...), searchapi_key: str = Form(...)):
    global summaries
    try:
        # Extract video ID
        video_id = extract_video_id(youtube_url)
        
        # Get transcript
        transcript, transcript_data = get_transcript(video_id, searchapi_key)
        
        # Get video title
        title = get_video_title(video_id)
        
        # Summarize transcript
        summary = summarize_with_deepseek(transcript, deepseek_api_key)
        
        # Create a unique ID for this summary
        summary_id = str(uuid.uuid4())
        
        # Store the summary
        summary_obj = Summary(
            id=summary_id,
            youtube_url=youtube_url,
            video_id=video_id,
            title=title,
            summary=summary,
            created_at=datetime.now().isoformat()
        )
        
        new_summary = summary_obj.dict()
  
        summaries.append(new_summary)
        save_summaries()
        
        # Return the summary page
        return templates.TemplateResponse(
            "index.html", 
            {
                "request": request, 
                "summary": summary,
                "youtube_url": youtube_url,
                "title": title,
                "active_tab": "generate"
            }
        )
    
    except HTTPException as e:
        print(f"HTTP Exception in summarize route: {e.status_code} - {e.detail}")
        return templates.TemplateResponse(
            "index.html", 
            {
                "request": request, 
                "error": e.detail,
                "youtube_url": youtube_url,
                "active_tab": "generate"
            }
        )
    except Exception as e:
        print(f"Unexpected error in summarize route: {type(e).__name__} - {str(e)}")
        print(traceback.format_exc())
        return templates.TemplateResponse(
            "index.html", 
            {
                "request": request, 
                "error": f"An unexpected error occurred: {str(e)}",
                "youtube_url": youtube_url,
                "active_tab": "generate"
            }
        )

@app.get("/history")
async def history(request: Request):
    global summaries
    return templates.TemplateResponse(
        "index.html", 
        {
            "request": request, 
            "summaries": summaries,
            "active_tab": "history"
        }
    )

@app.get("/api/summaries")
async def get_summaries():
    global summaries
    return {"summaries": summaries}

@app.get("/api/summary/{summary_id}")
async def get_summary(summary_id: str):
    global summaries
    for summary in summaries:
        if summary["id"] == summary_id:
            return summary.replace('\n', '<br>')
    raise HTTPException(status_code=404, detail="Summary not found")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
