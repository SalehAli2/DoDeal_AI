from pydantic import BaseModel


class User(BaseModel):
    name: str
    age: int

# This works — "25" (string) becomes 25 (int)
user = User(name="Ali", age="25")
print(user.age)  # 25 (int)



from pydantic import BaseModel, Field


class User(BaseModel):
    name: str = Field(min_length=2, max_length=50)
    age: int = Field(gt=0, lt=120)          # gt = greater than, lt = less than
    email: str = Field(default="unknown@example.com")
    bio: str = Field(default="", max_length=300, description="Short user bio")

from pydantic import SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    openai_api_key: SecretStr      # required, no default
    stripe_api_key: SecretStr
    api_base_url: str = "https://api.example.com"

    class Config:
        env_file = ".env"

settings = Settings()

"""
BaseSettings Is Perfect for API Keys/Secrets
API keys, tokens, and secrets should never be hardcoded in your code.
BaseSettings reads them from environment variables — exactly the same way as any other config.
This is actually one of the most common uses of BaseSettings in real projects.
"""

