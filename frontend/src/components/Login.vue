<template>
  <div>
    <b-loading is-full-page active></b-loading>
  </div>
</template>

<script>
import qs from 'qs'

export default {
  name: 'Login',
  async mounted() {
    let params = qs.parse(window.location.search, { ignoreQueryPrefix: true })
    if (typeof params.error !== 'undefined') {
      // TODO(james7132): Handle error
    } else if (typeof params.code !== 'undefined') {
      await this.$store.dispatch('login', params.code)
      if (params.state) {
        this.$router.push(window.atob(params.state))
        return
      }
    }
    this.$router.push('/')
  }
}
</script>

<style></style>
