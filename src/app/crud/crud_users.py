from fastcrud import FastCRUD

from ..models.user import User
from ..schemas.user import UserCreateInternal, UserDelete, UserReadInternal, UserUpdate, UserUpdateInternal

CRUDUser = FastCRUD[User, UserCreateInternal, UserUpdate, UserUpdateInternal, UserDelete, UserReadInternal]
crud_users = CRUDUser(User)
